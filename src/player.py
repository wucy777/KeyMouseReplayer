"""回放引擎。

要点：
- 独立线程 + 绝对时间轴（t0 + 累计dt/倍速），逐条绝对校准，不累积漂移。
- 等待用「可等待定时器 + WaitForMultipleObjects(timer, stop, pause, speed)」：
  暂停/停止/倍速修改都能从任意长度的间隔中毫秒级打断等待，不用等下一条
  事件到期——录制脚本里几分钟的长停顿不会让 F11/F12 失灵，挂机段也能随时
  调倍速提速。被打断的一侧会取消尚未触发的定时器，避免残留信号让下一次
  等待立即空过。
- 暂停/继续：暂停瞬间抬起所有按下的键与鼠标键并**记住按下集合**，继续时
  原样重新按下——避免游戏里出现「按键卡住」或「按住左键拖着鼠标」，
  也让「长按前进」这类动作在暂停干预后能接续。
- 时间轴在继续时重新对齐：暂停时长不计入，当前间隔重新等待。
- 倍速可在回放中实时调整：还没等完的剩余时间按新旧倍速等比换算，
  立即生效且不重走已等过的时间。
- 回放位置实时上报，可随时从「已执行到最后一条」的位置继续。
- 回放线程自身不做任何 UI 更新，进度回调按 20Hz 节流。
"""
from __future__ import annotations

import threading
import time

from . import winapi as W
from .script_io import STRIDE, Script

_BTN_DOWN_FLAGS = {
    W.BTN_LEFT: W.MOUSEEVENTF_LEFTDOWN,
    W.BTN_RIGHT: W.MOUSEEVENTF_RIGHTDOWN,
    W.BTN_MIDDLE: W.MOUSEEVENTF_MIDDLEDOWN,
    W.BTN_X1: W.MOUSEEVENTF_XDOWN,
    W.BTN_X2: W.MOUSEEVENTF_XDOWN,
}
_BTN_UP_FLAGS = {
    W.BTN_LEFT: W.MOUSEEVENTF_LEFTUP,
    W.BTN_RIGHT: W.MOUSEEVENTF_RIGHTUP,
    W.BTN_MIDDLE: W.MOUSEEVENTF_MIDDLEUP,
    W.BTN_X1: W.MOUSEEVENTF_XUP,
    W.BTN_X2: W.MOUSEEVENTF_XUP,
}
_BTN_DATA = {W.BTN_X1: W.XBUTTON1, W.BTN_X2: W.XBUTTON2}


class Player:
    def __init__(self):
        self._thread: threading.Thread | None = None
        # Win32 手动复位事件：要与定时器句柄一起进 WaitForMultipleObjects。
        # _resume：set = 播放中，clear = 已暂停（状态语义）。
        # _pause_req：平时无信号，pause() 置位——它是「事件」而不是「状态」，
        #   因为 WaitForMultipleObjects 只认受信不认跳变，拿 _resume 进等待集
        #   会让播放中的每次等待立即返回（空转 bug）。
        self._stop = W.WinEvent()
        self._pause_req = W.WinEvent()
        self._speed_req = W.WinEvent()   # 倍速变更信号：等待中也要立即生效
        self._resume = W.WinEvent(initial=True)
        self._lock = threading.Lock()
        self._script: Script | None = None
        self.speed = 1.0
        self.start_index = 0
        self.on_progress = None   # (executed_index, total) -> None
        self.on_finish = None     # (reason:str, executed_index:int) -> None
        self._executed = 0
        self._total = 0
        self._playing = False
        self._last_report = 0.0
        # 干跑：只走时间轴与状态机，不真正注入输入（自检 / 预演用）
        self.dry_run = False
        self._waiter = W.PreciseWait()
        # 暂停时记下的按下集合：({vk: (scan, flags)}, {btn, ...})，继续时重按
        self._held: tuple[dict[int, tuple[int, int]], set[int]] = ({}, set())

    # ------------------------------------------------------------ 状态
    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def paused(self) -> bool:
        return self._playing and not self._resume.is_set()

    @property
    def executed(self) -> int:
        with self._lock:
            return self._executed

    @property
    def total(self) -> int:
        return self._total

    # ------------------------------------------------------------ 控制
    def play(self, script: Script, start_index: int = 0, speed: float = 1.0) -> None:
        if self._playing:
            return
        self._script = script
        self.speed = max(0.05, min(20.0, float(speed)))
        self.start_index = max(0, min(int(start_index), script.count))
        self._total = script.count
        with self._lock:
            self._executed = self.start_index
        self._stop.clear()
        self._pause_req.clear()
        self._speed_req.clear()
        self._resume.set()
        self._last_report = 0.0
        self._playing = True
        self._thread = threading.Thread(target=self._run, name="player", daemon=True)
        self._thread.start()

    def notify_speed_change(self) -> None:
        """倍速已修改；等待中的回放线程会立即醒来重算时刻。"""
        if self._playing:
            self._speed_req.set()

    def pause(self) -> None:
        """请求暂停；钩子线程也会调用，绝不可阻塞。"""
        if self._playing:
            self._resume.clear()
            self._pause_req.set()

    def resume(self) -> None:
        if self._playing:
            self._resume.set()
            self._pause_req.clear()

    def toggle_pause(self) -> None:
        if not self._playing:
            return
        if self._resume.is_set():
            self.pause()
        else:
            self.resume()

    def stop(self) -> None:
        if self._playing:
            self._stop.set()
            self._resume.set()

    def wait(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)

    def close(self) -> None:
        """释放内核句柄。确认不再播放后调用（等待中调用会破坏等待）。"""
        self._waiter.close()
        if not self._playing:
            self._stop.close()
            self._pause_req.close()
            self._speed_req.close()
            self._resume.close()

    # ------------------------------------------------------------ 主循环
    def _run(self) -> None:
        script = self._script
        if script is None:
            self._playing = False
            return
        ev = script.events
        n = script.count
        idx = self.start_index
        speed = self.speed
        # 只有在拿不到高精度定时器时才抬系统计时器精度：
        # timeBeginPeriod 会抬高全系统定时器中断频率，对正在跑的程序是实打实的开销。
        raised_timer = False
        if not self._waiter.available:
            W.begin_timer_precision()
            raised_timer = True
        perf = time.perf_counter

        down_keys: dict[int, tuple[int, int]] = {}   # vk -> (scan, flags)
        down_buttons: set[int] = set()
        reason = "finished"
        t0 = perf()
        cum_us = 0
        dry = self.dry_run
        try:
            while idx < n:
                if self._stop.is_set():
                    reason = "stopped"
                    break

                if not self._resume.is_set():
                    t0 = self._pause_cycle(idx, n, down_keys, down_buttons, cum_us, speed, dry)
                    if t0 is None:
                        reason = "stopped"
                        break

                base = idx * STRIDE
                kind = ev[base]
                # cum_anchor：已执行事件的时间轴累计（不含当前正在等待的这条），
                # 暂停继续后据此重对齐，当前间隔重新完整等待
                cum_anchor = cum_us
                cum_us += ev[base + 4]

                # 等待到当前事件的绝对时刻。内循环处理等待中途的干预：
                # - 倍速修改：还没等完的剩余时间按新旧倍速等比换算，立即生效
                #   且不重走已等过的时间
                # - 暂停：释放按住的键 → 等继续 → 重按 → 当前间隔重新完整等待
                # - 停止：立即退出
                target = t0 + cum_us / 1e6 / speed
                stopped = False
                while True:
                    sp = self.speed
                    if sp != speed:
                        now = perf()
                        rem = target - now
                        if rem < 0.0:
                            rem = 0.0
                        target = now + rem * speed / sp
                        t0 = target - cum_us / 1e6 / sp
                        speed = sp
                    now = perf()
                    if target <= now:
                        break
                    woken = self._sleep_until(target, now)
                    if woken is None:
                        break
                    if woken == "stop":
                        reason = "stopped"
                        stopped = True
                        break
                    if woken == "speed":
                        self._speed_req.clear()
                        continue
                    # woken == "pause"：等继续/停止，重对齐后重算当前事件时刻
                    t0 = self._pause_cycle(idx, n, down_keys, down_buttons,
                                           cum_anchor, speed, dry)
                    if t0 is None:
                        reason = "stopped"
                        stopped = True
                        break
                    target = t0 + cum_us / 1e6 / speed
                if stopped:
                    break

                if dry:
                    pass
                elif kind == W.K_MOUSE_MOVE:
                    self._send_move(ev[base + 1], ev[base + 2])
                elif kind == W.K_BUTTON_DOWN:
                    btn = ev[base + 1]
                    self._send_button(btn, True, ev[base + 2], ev[base + 3])
                    down_buttons.add(btn)
                elif kind == W.K_BUTTON_UP:
                    btn = ev[base + 1]
                    self._send_button(btn, False, ev[base + 2], ev[base + 3])
                    down_buttons.discard(btn)
                elif kind == W.K_WHEEL:
                    self._send_wheel(ev[base + 1], W.MOUSEEVENTF_WHEEL, ev[base + 2], ev[base + 3])
                elif kind == W.K_HWHEEL:
                    self._send_wheel(ev[base + 1], W.MOUSEEVENTF_HWHEEL, ev[base + 2], ev[base + 3])
                elif kind == W.K_KEY_DOWN:
                    vk = ev[base + 1]
                    sc = ev[base + 2]
                    ext = ev[base + 3]
                    if not sc:
                        sc = W.vk_to_scan(vk)
                    flags = W.KEYEVENTF_SCANCODE | (W.KEYEVENTF_EXTENDEDKEY if ext else 0)
                    W.send_inputs([W.make_key_input(vk, sc, flags)])
                    down_keys[vk] = (sc, flags)
                elif kind == W.K_KEY_UP:
                    vk = ev[base + 1]
                    sc = ev[base + 2]
                    ext = ev[base + 3]
                    if not sc:
                        sc = W.vk_to_scan(vk)
                    flags = W.KEYEVENTF_SCANCODE | (W.KEYEVENTF_EXTENDEDKEY if ext else 0)
                    W.send_inputs([W.make_key_input(vk, sc, flags | W.KEYEVENTF_KEYUP)])
                    down_keys.pop(vk, None)
                else:
                    pass

                idx += 1
                self._set_executed(idx)
                self._report(idx, n)
        except Exception as exc:  # 保证异常时也能收尾
            reason = f"error: {exc}"
        finally:
            # 收尾：释放所有仍按下的键与鼠标键（含暂停重按过的）
            if down_keys and not dry:
                batch = [W.make_key_input(vk, sc, fl | W.KEYEVENTF_KEYUP)
                         for vk, (sc, fl) in down_keys.items()]
                W.send_inputs(batch)
            if down_buttons and not dry:
                batch = [W.make_mouse_input(0, 0, _BTN_DATA.get(b, 0),
                                            _BTN_UP_FLAGS.get(b, W.MOUSEEVENTF_XUP))
                         for b in down_buttons]
                W.send_inputs(batch)
            if raised_timer:
                W.end_timer_precision()
            self._waiter.close()
            self._playing = False
            self._report(idx, n, force=True)
            cb = self.on_finish
            if cb is not None:
                try:
                    cb(reason, idx)
                except Exception:
                    pass

    # ------------------------------------------------------------ 暂停
    def _pause_cycle(self, idx: int, n: int, down_keys: dict, down_buttons: set,
                     cum_anchor_us: int, speed: float, dry: bool) -> float | None:
        """暂停期间的统一处理：释放按住的键 → 等待继续/停止 → 重对齐 → 重按。

        cum_anchor_us 是已执行事件的时间轴累计，不含正在等待的这条事件，
        因此继续后当前间隔重新完整等待，暂停时长不计入时间轴。
        返回新的 t0；返回 None 表示等待期间收到了停止请求。
        """
        self._release_held(down_keys, down_buttons, dry)
        self._report(idx, n, force=True)
        if not (self._resume.h and self._stop.h):
            # 内核事件句柄创建失败（几乎不会发生）：退化成小睡轮询，绝不空转
            while True:
                if self._stop.is_set():
                    return None
                if not self._resume.h or self._resume.is_set():
                    break
                time.sleep(0.02)
        else:
            r = W.wait_any([self._resume.h, self._stop.h])
            if r == W.WAIT_OBJECT_0 + 1:
                return None
        t0 = time.perf_counter() - cum_anchor_us / 1e6 / speed
        self._repress_held(down_keys, down_buttons, dry)
        return t0

    def _release_held(self, down_keys: dict, down_buttons: set, dry: bool) -> None:
        """抬起当前按住的所有键/鼠标键，并把集合记到 self._held 供继续时重按。"""
        ks = dict(down_keys)
        bs = set(down_buttons)
        down_keys.clear()
        down_buttons.clear()
        self._held = (ks, bs)
        if dry or not (ks or bs):
            return
        batch = [W.make_key_input(vk, sc, fl | W.KEYEVENTF_KEYUP)
                 for vk, (sc, fl) in ks.items()]
        for b in bs:
            batch.append(W.make_mouse_input(0, 0, _BTN_DATA.get(b, 0),
                                            _BTN_UP_FLAGS.get(b, W.MOUSEEVENTF_XUP)))
        W.send_inputs(batch)

    def _repress_held(self, down_keys: dict, down_buttons: set, dry: bool) -> None:
        """继续时把暂停前按着的键/鼠标键按原按下顺序重新按下。"""
        ks, bs = self._held
        self._held = ({}, set())
        if not ks and not bs:
            return
        down_keys.update(ks)
        down_buttons.update(bs)
        if dry:
            return
        # 按键重按不带 KEYUP；鼠标重按不带 MOVE|ABSOLUTE，不挪动光标
        batch = [W.make_key_input(vk, sc, fl) for vk, (sc, fl) in ks.items()]
        for b in bs:
            batch.append(W.make_mouse_input(0, 0, _BTN_DATA.get(b, 0),
                                            _BTN_DOWN_FLAGS.get(b, W.MOUSEEVENTF_XDOWN)))
        W.send_inputs(batch)

    # ------------------------------------------------------------ 工具
    # 定时器唤醒本身约有 0.5ms 的固定迟滞，末段自旋只做收口。
    # 实测（4000 条 × 4ms）：自旋 0.2ms 时落点中位 0.16ms、P99 0.5ms，CPU 约 1.4%；
    # 自旋 0.5ms 能把落点压到 0.04ms，但 CPU 升到 6.8%，不划算。
    # 时间轴按绝对时刻推进，单条迟到不累积，因此不需要更大的自旋窗口。
    SPIN_TAIL_S = 0.0002

    def _sleep_until(self, target: float, now: float) -> str | None:
        """睡到绝对时刻 target；等待可被 stop / resume 事件提前打断。

        返回 None 表示正常到点，"stop" / "pause" 表示被对应事件提前唤醒。
        """
        remaining = target - now
        if remaining > self.SPIN_TAIL_S:
            r = self._waiter.wait_interruptible(
                remaining - self.SPIN_TAIL_S, self._stop.h, self._pause_req.h,
                self._speed_req.h)
            if r == 1:
                return "stop"
            if r == 2:
                return "pause"
            if r == 3:
                return "speed"
        while time.perf_counter() < target:
            pass
        return None

    @staticmethod
    def _send_move(x: int, y: int) -> None:
        ax, ay = W.to_absolute(x, y)
        W.send_inputs([W.make_mouse_input(
            ax, ay, 0,
            W.MOUSEEVENTF_MOVE | W.MOUSEEVENTF_ABSOLUTE | W.MOUSEEVENTF_VIRTUALDESK,
        )])

    @staticmethod
    def _send_wheel(delta: int, flag: int, x: int, y: int) -> None:
        """下发滚轮事件。

        SendInput 的 mouseData 按「有符号 32 位整数」解释滚轮增量，并会被系统
        夹到 ±32767；实测把它拆成高/低两个 16 位字写进去会被当成越界值而夹成
        32767，滚轮方向与格数全错。正确做法是直接写入有符号增量本身
        （例如 +120 写 0x00000078，-120 写 0xFFFFFF88）。

        注意：钩子回调那边拿到的事件里增量位于高 16 位，两者约定不同，
        所以录制与回放不能共用同一套编码。
        """
        data = delta & 0xFFFFFFFF
        ax, ay = W.to_absolute(x, y)
        W.send_inputs([W.make_mouse_input(
            ax, ay, data,
            flag | W.MOUSEEVENTF_MOVE | W.MOUSEEVENTF_ABSOLUTE | W.MOUSEEVENTF_VIRTUALDESK,
        )])

    @staticmethod
    def _send_button(btn: int, down: bool, x: int, y: int) -> None:
        ax, ay = W.to_absolute(x, y)
        table = _BTN_DOWN_FLAGS if down else _BTN_UP_FLAGS
        flag = table.get(btn, W.MOUSEEVENTF_XDOWN if down else W.MOUSEEVENTF_XUP)
        W.send_inputs([W.make_mouse_input(
            ax, ay, _BTN_DATA.get(btn, 0),
            flag | W.MOUSEEVENTF_MOVE | W.MOUSEEVENTF_ABSOLUTE | W.MOUSEEVENTF_VIRTUALDESK,
        )])

    def _set_executed(self, idx: int) -> None:
        with self._lock:
            self._executed = idx

    def _report(self, idx: int, total: int, force: bool = False) -> None:
        cb = self.on_progress
        if cb is None:
            return
        now = time.perf_counter()
        if not force and now - self._last_report < 0.05:
            return
        self._last_report = now
        try:
            cb(idx, total)
        except Exception:
            pass
