"""录制引擎：低级键鼠钩子 -> 紧凑事件缓冲。

内存方案：
- 事件不建对象，直接写入预分配的 array('i')（每条 6 个 int32 = 24 字节）。
- 块式存储，绝不整表复制扩容；上万条事件约 0.24MB，百万条约 24MB。
- 记录期间不落盘、不格式化、无 GC 压力。

鼠标移动降采样（时间/距离阈值）由 UI 层调参后写入，默认开启。
"""
from __future__ import annotations

import array
import ctypes
import threading
import time

from . import winapi as W

# 事件在数组中的字段布局：kind, a, b, c, dt_us, flags
STRIDE = 6

# 录制事件保护上限：16M 条 ≈ 384 MB 内存（保存时瞬时峰值约为 2 倍）。
# 按 8ms 移动采样算，连续高速录制约 36 小时才会触顶；触顶后停止记录并提示。
MAX_EVENTS = 16_000_000

# dt_us 是 i32（上限约 35.8 分钟）。录制时超过 30 分钟的停顿按 30 分钟记录：
# 既是防溢出截断，也是防异常时间戳的保护。脚本统计的「总时长」按截断后计。
MAX_GAP_US = 1_800_000_000

# 钩子线程的自定义消息：鼠标钩子的装卸请求经 PostThreadMessageW 投递到这里执行
_WM_INSTALL_MOUSE = W.WM_APP + 1
_WM_UNINSTALL_MOUSE = W.WM_APP + 2


class Recorder:
    """在专用线程上跑消息循环并安装钩子。"""

    def __init__(self, on_event=None, move_min_interval_ms: int = 8, move_min_distance_px: int = 2):
        self._lock = threading.Lock()
        self._thread_id = 0
        self._thread: threading.Thread | None = None
        self._kb_hook = None
        self._ms_hook = None
        self._kb_proc = None
        self._ms_proc = None
        self._running = False
        self._recording = False

        self._buf = array.array("i")
        self._count = 0
        self._last_us = 0
        self._overflow = False

        self._last_mx = -1
        self._last_my = -1
        self._last_move_us = 0

        self.move_min_interval_us = max(0, move_min_interval_ms) * 1000
        self.move_min_distance = max(0, move_min_distance_px)

        self.on_event = on_event
        self.on_state_change = None
        # 默认忽略本程序自己注入的事件（否则回放会被自己录进去）。
        # 自检脚本会临时打开它来验证钩子链路。
        self.allow_injected = False
        self._hotkeys: dict[int, tuple[int, object]] = {}
        self._hotkey_enabled = False
        self._hotkey_held: set[int] = set()
        self._ready = threading.Event()
        # 鼠标钩子装卸请求的完成信号（装卸在钩子线程内执行）
        self._ms_req = threading.Event()
        self._ms_ok = False
        self._pump_running = False

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._ready.clear()
        self._thread = threading.Thread(target=self._pump, name="input-hook", daemon=True)
        self._thread.start()
        self._ready.wait(3.0)

    @property
    def kb_hook_ok(self) -> bool:
        """常驻键盘钩子是否安装成功（失败则快捷键和键盘录制都不可用）。"""
        return self._kb_hook is not None

    def stop(self) -> None:
        if not self._running:
            return
        self._recording = False
        self._running = False
        tid = self._thread_id
        if tid:
            W.user32.PostThreadMessageW(tid, W.WM_QUIT, 0, 0)
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(2.0)
        self._thread = None

    # ------------------------------------------------------------ 快捷键
    def set_hotkeys(self, mapping: dict[int, tuple[int, object]], enabled: bool = True) -> None:
        """mapping: vk -> (modifier_mask, callback)"""
        self._hotkeys = mapping
        self._hotkey_enabled = enabled

    def set_hotkey_enabled(self, enabled: bool) -> None:
        self._hotkey_enabled = enabled

    # ------------------------------------------------------------ 录制控制
    def start_recording(self) -> None:
        with self._lock:
            self._buf = array.array("i")
            self._count = 0
            self._last_us = time.perf_counter_ns() // 1000
            self._overflow = False
            self._last_mx = -1
            self._last_my = -1
            self._last_move_us = 0
            self._recording = True
        W.refresh_virtual_screen()
        try:
            self._install_mouse_hook_on_pump()
        except OSError:
            with self._lock:
                self._recording = False
            raise
        if self.on_state_change:
            self.on_state_change(True)

    def stop_recording(self) -> None:
        with self._lock:
            was = self._recording
            self._recording = False
        if self._ms_hook:
            self._ms_req.clear()
            posted = self._pump_running and W.user32.PostThreadMessageW(
                self._thread_id, _WM_UNINSTALL_MOUSE, 0, 0)
            if posted:
                self._ms_req.wait(2.0)
            if self._ms_hook:  # 钩子线程没响应（如已退出）时兜底：跨线程卸载是安全的
                self._uninstall_mouse_hook()
        if was and self.on_state_change:
            self.on_state_change(False)

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def count(self) -> int:
        return self._count

    @property
    def overflowed(self) -> bool:
        return self._overflow

    def take_events(self) -> tuple[array.array, int]:
        """取出并清空缓冲（录制停止后调用）。

        返回的数组会裁到实际长度：缓冲是分块追加的，容量通常大于实际条数，
        直接用 tobytes() 会把多余容量也写进文件。
        """
        with self._lock:
            cnt = self._count
            buf = self._buf[: cnt * STRIDE]
            self._buf = array.array("i")
            self._count = 0
        return buf, cnt

    # ------------------------------------------------------------ 内部
    def _install_mouse_hook(self) -> None:
        if self._ms_hook:
            return
        self._ms_proc = W.MSHOOKPROC(self._mouse_cb)
        self._ms_hook = W.user32.SetWindowsHookExW(
            W.WH_MOUSE_LL, ctypes.cast(self._ms_proc, ctypes.c_void_p), None, 0
        )
        if not self._ms_hook:
            self._ms_hook = None
            raise OSError("安装鼠标钩子失败，请尝试以管理员身份运行")

    def _uninstall_mouse_hook(self) -> None:
        if self._ms_hook:
            W.user32.UnhookWindowsHookEx(self._ms_hook)
            self._ms_hook = None
            self._ms_proc = None

    def _install_mouse_hook_on_pump(self) -> None:
        """在钩子线程内安装鼠标钩子（装卸请求经线程消息投递）。

        低级钩子的回调只在安装它的线程泵消息时触发：装在 UI 线程上，
        主线程一卡（打开大脚本、模态框、统计计算）钩子就会超时丢事件。
        钩子线程跑着 GetMessageW 死循环，回调永远能及时执行。
        """
        if self._ms_hook:
            return
        if not self._pump_running or not self._thread_id:
            raise OSError("钩子线程未在运行，无法安装鼠标钩子")
        self._ms_req.clear()
        self._ms_ok = False
        if not W.user32.PostThreadMessageW(self._thread_id, _WM_INSTALL_MOUSE, 0, 0):
            raise OSError("无法向钩子线程发送安装请求")
        if not self._ms_req.wait(3.0) or not self._ms_ok:
            raise OSError("安装鼠标钩子失败，请尝试以管理员身份运行")

    def _pump(self) -> None:
        self._thread_id = W.kernel32.GetCurrentThreadId()
        self._kb_proc = W.KBHOOKPROC(self._keyboard_cb)
        self._kb_hook = W.user32.SetWindowsHookExW(
            W.WH_KEYBOARD_LL, ctypes.cast(self._kb_proc, ctypes.c_void_p), None, 0
        )
        if not self._kb_hook:
            self._ready.set()
            return
        self._pump_running = True
        self._ready.set()
        msg = ctypes.wintypes.MSG()
        # GetMessageW 阻塞，零 CPU 占用；WM_QUIT 退出
        while True:
            r = W.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            m = msg.message
            if m == _WM_INSTALL_MOUSE:
                try:
                    self._install_mouse_hook()
                    self._ms_ok = True
                except OSError:
                    self._ms_ok = False
                self._ms_req.set()
            elif m == _WM_UNINSTALL_MOUSE:
                self._uninstall_mouse_hook()
                self._ms_req.set()
            else:
                W.user32.TranslateMessage(ctypes.byref(msg))
                W.user32.DispatchMessageW(ctypes.byref(msg))
        self._pump_running = False
        self._uninstall_mouse_hook()
        if self._kb_hook:
            W.user32.UnhookWindowsHookEx(self._kb_hook)
            self._kb_hook = None
            self._kb_proc = None

    # ------------------------------------------------------------ 钩子回调
    def _keyboard_cb(self, ncode, wparam, lparam):
        if ncode == 0:
            info = lparam.contents
            msg = wparam
            if msg in (W.WM_KEYDOWN, W.WM_SYSKEYDOWN, W.WM_KEYUP, W.WM_SYSKEYUP):
                flags = info.flags
                injected = bool(flags & (W.LLKHF_INJECTED | W.LLKHF_LOWER_IL_INJECTED))
                down = msg in (W.WM_KEYDOWN, W.WM_SYSKEYDOWN)
                vk = int(info.vkCode)
                # 快捷键分发（回放注入的事件不触发快捷键，避免自激）
                if not injected and self._hotkey_enabled:
                    if down:
                        if vk not in self._hotkey_held:
                            self._hotkey_held.add(vk)
                            hk = self._hotkeys.get(vk)
                            if hk is not None:
                                mask, cb = hk
                                if mask == 0 or (W.modifier_state() & mask) == mask:
                                    try:
                                        cb()
                                    except Exception:
                                        pass
                    else:
                        self._hotkey_held.discard(vk)
                if self._recording and (self.allow_injected or not injected):
                    # 快捷键本身不录入；带修饰键的快捷键只在组合真正匹配时才排除，
                    # 这样 Ctrl+F9 作快捷键时仍能录到裸按的 F9
                    hk = self._hotkeys.get(vk)
                    hit = False
                    if hk is not None:
                        mask = hk[0]
                        hit = mask == 0 or (W.modifier_state() & mask) == mask
                    if not hit:
                        self._push(
                            W.K_KEY_DOWN if down else W.K_KEY_UP,
                            vk,
                            int(info.scanCode) & 0xFF,
                            0x01 if (flags & W.LLKHF_EXTENDED) else 0,
                            0,
                        )
        return W.user32.CallNextHookEx(None, ncode, wparam, ctypes.cast(lparam, ctypes.c_void_p))

    def _mouse_cb(self, ncode, wparam, lparam):
        if ncode == 0 and self._recording:
            info = lparam.contents
            flags = info.flags
            if self.allow_injected or not (flags & (W.LLMHF_INJECTED | W.LLMHF_LOWER_IL_INJECTED)):
                msg = wparam
                if msg == W.WM_MOUSEMOVE:
                    self._on_move(info.pt.x, info.pt.y)
                elif msg == W.WM_LBUTTONDOWN:
                    self._push(W.K_BUTTON_DOWN, W.BTN_LEFT, info.pt.x, info.pt.y, 0)
                elif msg == W.WM_LBUTTONUP:
                    self._push(W.K_BUTTON_UP, W.BTN_LEFT, info.pt.x, info.pt.y, 0)
                elif msg == W.WM_RBUTTONDOWN:
                    self._push(W.K_BUTTON_DOWN, W.BTN_RIGHT, info.pt.x, info.pt.y, 0)
                elif msg == W.WM_RBUTTONUP:
                    self._push(W.K_BUTTON_UP, W.BTN_RIGHT, info.pt.x, info.pt.y, 0)
                elif msg == W.WM_MBUTTONDOWN:
                    self._push(W.K_BUTTON_DOWN, W.BTN_MIDDLE, info.pt.x, info.pt.y, 0)
                elif msg == W.WM_MBUTTONUP:
                    self._push(W.K_BUTTON_UP, W.BTN_MIDDLE, info.pt.x, info.pt.y, 0)
                elif msg == W.WM_XBUTTONDOWN or msg == W.WM_XBUTTONUP:
                    xbtn = (info.mouseData >> 16) & 0xFFFF
                    btn = W.BTN_X1 if xbtn == W.XBUTTON1 else W.BTN_X2
                    self._push(
                        W.K_BUTTON_DOWN if msg == W.WM_XBUTTONDOWN else W.K_BUTTON_UP,
                        btn, info.pt.x, info.pt.y, 0,
                    )
                elif msg == W.WM_MOUSEWHEEL:
                    # 钩子里滚轮增量在高 16 位（与 SendInput 的有符号 32 位约定不同）
                    delta = ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value
                    self._push(W.K_WHEEL, delta, info.pt.x, info.pt.y, 0)
                elif msg == W.WM_MOUSEHWHEEL:
                    delta = ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value
                    self._push(W.K_HWHEEL, delta, info.pt.x, info.pt.y, 0)
        return W.user32.CallNextHookEx(None, ncode, wparam, ctypes.cast(lparam, ctypes.c_void_p))

    def _on_move(self, x: int, y: int) -> None:
        now = time.perf_counter_ns() // 1000
        if self.move_min_interval_us and (now - self._last_move_us) < self.move_min_interval_us:
            return
        if self.move_min_distance:
            dx = x - self._last_mx
            dy = y - self._last_my
            if self._last_mx >= 0 and (dx * dx + dy * dy) < (self.move_min_distance * self.move_min_distance):
                return
        self._last_move_us = now
        self._last_mx = x
        self._last_my = y
        self._push(W.K_MOUSE_MOVE, x, y, 0, 0)

    def _push(self, kind: int, a: int, b: int, c: int, extra: int) -> None:
        now = time.perf_counter_ns() // 1000
        dt = now - self._last_us
        if dt < 0:
            dt = 0
        self._last_us = now
        if dt > MAX_GAP_US:  # 超过 30 分钟的停顿按 30 分钟记录（i32 防溢出截断）
            dt = MAX_GAP_US
        with self._lock:
            if self._count >= MAX_EVENTS:
                self._overflow = True
                return
            buf = self._buf
            buf.append(kind)
            buf.append(a)
            buf.append(b)
            buf.append(c)
            buf.append(dt)
            buf.append(extra)
            self._count += 1
        cb = self.on_event
        if cb is not None:
            cb(self._count, kind, a, b)
