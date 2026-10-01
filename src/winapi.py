"""Win32 底层封装：低级键鼠钩子、SendInput 注入、输入状态查询。

设计要点（性能优先）：
- 所有结构体 / 函数原型只在模块导入时定义一次。
- 钩子回调内只做最少的 ctypes 取值，绝不做格式化、IO、日志。
- 鼠标钩子按需安装（仅在录制中），键盘钩子常驻（用于全局快捷键）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w

# ---------------------------------------------------------------- DLL 句柄
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

try:
    winmm = ctypes.WinDLL("winmm", use_last_error=True)
except OSError:  # pragma: no cover
    winmm = None

HHOOK = ctypes.c_void_p
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

# ---------------------------------------------------------------- 常量
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
WM_APP = 0x8000

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_MOUSEHWHEEL = 0x020E

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLKHF_LOWER_IL_INJECTED = 0x02
LLMHF_INJECTED = 0x01
LLMHF_LOWER_IL_INJECTED = 0x02

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_UNICODE = 0x0004

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

MAPVK_VK_TO_VSC = 0
MAPVK_VSC_TO_VK_EX = 3

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_ESCAPE = 0x1B

# ---------------------------------------------------------------- 事件类型编码（落盘 / 内存统一）
K_KEY_DOWN = 0
K_KEY_UP = 1
K_MOUSE_MOVE = 2
K_BUTTON_DOWN = 3
K_BUTTON_UP = 4
K_WHEEL = 5
K_HWHEEL = 6
K_MOUSE_REL = 7

KIND_NAMES = {
    K_KEY_DOWN: "按键按下",
    K_KEY_UP: "按键抬起",
    K_MOUSE_MOVE: "鼠标移动",
    K_BUTTON_DOWN: "鼠标按下",
    K_BUTTON_UP: "鼠标抬起",
    K_WHEEL: "滚轮",
    K_HWHEEL: "横向滚轮",
    K_MOUSE_REL: "相对移动",
}

BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_X1, BTN_X2 = 0, 1, 2, 3, 4
BTN_NAMES = {BTN_LEFT: "左键", BTN_RIGHT: "右键", BTN_MIDDLE: "中键", BTN_X1: "侧键1", BTN_X2: "侧键2"}


# ---------------------------------------------------------------- 结构体
class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", w.DWORD),
        ("scanCode", w.DWORD),
        ("flags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", w.POINT),
        ("mouseData", w.DWORD),
        ("flags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", w.LONG),
        ("dy", w.LONG),
        ("mouseData", w.DWORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", w.WORD),
        ("wScan", w.WORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]


# 32 位 Python 下 INPUT 是 28 字节，SendInput 的参数全部错位。
# 不能用 assert：build.spec 开了 optimize=1，assert 会被整体剥掉。
if ctypes.sizeof(INPUT) != 40:
    raise RuntimeError(
        f"INPUT 结构体大小异常（{ctypes.sizeof(INPUT)} 字节）：本程序仅支持 64 位 Python"
    )

KBHOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, ctypes.POINTER(KBDLLHOOKSTRUCT))
MSHOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, ctypes.POINTER(MSLLHOOKSTRUCT))

# ---------------------------------------------------------------- 函数原型
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, w.HINSTANCE, w.DWORD]
user32.SetWindowsHookExW.restype = HHOOK
user32.UnhookWindowsHookEx.argtypes = [HHOOK]
user32.UnhookWindowsHookEx.restype = w.BOOL
user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, w.WPARAM, ctypes.c_void_p]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
user32.PeekMessageW.restype = w.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
user32.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
user32.PostThreadMessageW.restype = w.BOOL
user32.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = w.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.MapVirtualKeyW.argtypes = [w.UINT, w.UINT]
user32.MapVirtualKeyW.restype = w.UINT

kernel32.GetCurrentThreadId.restype = w.DWORD

# ---------------------------------------------------------------- 高精度等待定时器
# 用可等待定时器做亚毫秒休眠，替代「睡一觉再自旋」的写法：
# 等待期间线程真正挂起，CPU 占用为 0，精度仍然够（Win10 1803+ 支持高精度模式）。
CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x00000002
TIMER_ALL_ACCESS = 0x001F0003
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF

kernel32.CreateWaitableTimerExW.argtypes = [ctypes.c_void_p, w.LPCWSTR, w.DWORD, w.DWORD]
kernel32.CreateWaitableTimerExW.restype = ctypes.c_void_p
kernel32.SetWaitableTimer.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_longlong),
                                      w.LONG, ctypes.c_void_p, ctypes.c_void_p, w.BOOL]
kernel32.SetWaitableTimer.restype = w.BOOL
kernel32.CancelWaitableTimer.argtypes = [ctypes.c_void_p]
kernel32.CancelWaitableTimer.restype = w.BOOL
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, w.DWORD]
kernel32.WaitForSingleObject.restype = w.DWORD
kernel32.WaitForMultipleObjects.argtypes = [w.DWORD, ctypes.POINTER(ctypes.c_void_p), w.BOOL, w.DWORD]
kernel32.WaitForMultipleObjects.restype = w.DWORD
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
kernel32.CreateEventW.restype = ctypes.c_void_p
kernel32.SetEvent.argtypes = [ctypes.c_void_p]
kernel32.SetEvent.restype = w.BOOL
kernel32.ResetEvent.argtypes = [ctypes.c_void_p]
kernel32.ResetEvent.restype = w.BOOL
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = w.BOOL


class WinEvent:
    """Win32 手动复位事件。

    回放的暂停/停止信号要和可等待定时器句柄一起进 WaitForMultipleObjects，
    threading.Event 没有可等待的内核句柄，所以用这个。
    """

    __slots__ = ("h",)

    def __init__(self, initial: bool = False) -> None:
        self.h = kernel32.CreateEventW(None, True, bool(initial), None)

    def set(self) -> None:
        if self.h:
            kernel32.SetEvent(self.h)

    def clear(self) -> None:
        if self.h:
            kernel32.ResetEvent(self.h)

    def is_set(self) -> bool:
        return bool(self.h) and kernel32.WaitForSingleObject(self.h, 0) == WAIT_OBJECT_0

    def close(self) -> None:
        if self.h:
            kernel32.CloseHandle(self.h)
            self.h = None

    def __del__(self) -> None:  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass


def wait_any(handles: list, timeout_ms: int = INFINITE) -> int:
    """等待任一内核句柄受信，返回 WaitForMultipleObjects 的原始返回值。

    句柄为 None 会立即返回 WAIT_FAILED，调用方需自行兜底。
    """
    n = len(handles)
    arr = (ctypes.c_void_p * n)(*handles)
    return int(kernel32.WaitForMultipleObjects(n, arr, False, timeout_ms))


class PreciseWait:
    """可复用的高精度定时器；创建失败时自动回退到轮询等待。

    每个回放线程持有自己的实例（定时器是线程可见的内核对象，但串行使用即可）。
    """

    __slots__ = ("_h", "_lazy", "_due", "_high_res", "_harr")

    def __init__(self) -> None:
        self._h = None
        self._lazy = False
        self._due = ctypes.c_longlong(0)
        self._high_res = False
        self._harr = (ctypes.c_void_p * 4)()

    def _ensure(self) -> bool:
        if self._h is not None:
            return True
        if self._lazy:
            return False
        h = kernel32.CreateWaitableTimerExW(
            None, None, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS
        )
        self._high_res = bool(h)
        if not h:
            h = kernel32.CreateWaitableTimerExW(None, None, 0, TIMER_ALL_ACCESS)
        if not h:
            self._lazy = True
            return False
        self._h = h
        return True

    @property
    def available(self) -> bool:
        """高精度定时器是否可用（不可用时调用方才会去抬系统计时器精度）。"""
        if not self._ensure():
            return False
        return self._high_res

    def wait_interruptible(self, seconds: float, stop_handle, pause_handle,
                           speed_handle=None) -> int:
        """等待 seconds 秒，可被 stop / pause / speed 事件提前打断。

        三个句柄都是「平时无信号、请求时置位」的手动复位事件。
        返回 0=定时到点，1=stop 事件，2=pause 事件（暂停请求），3=speed 事件
        （倍速变更，调用方应重算时刻后继续等）。
        长短间隔都走定时器（等待期间线程挂起，CPU 为 0），因此脚本里几分钟的
        长停顿中按暂停/停止/调倍速也能毫秒级生效；被打断的一侧必须取消尚未
        触发的定时器，否则残留信号会让下一次等待立即空过。
        """
        if seconds <= 0:
            return 0
        if not self._ensure() or not stop_handle or not pause_handle:
            return self._wait_polled(seconds, stop_handle, pause_handle, speed_handle)
        # SetWaitableTimer 用 100ns 为单位，负值表示相对时间
        self._due.value = -int(seconds * 10_000_000)
        if not kernel32.SetWaitableTimer(self._h, ctypes.byref(self._due), 0, None, None, False):
            return self._wait_polled(seconds, stop_handle, pause_handle, speed_handle)
        arr = self._harr
        arr[0] = self._h
        arr[1] = stop_handle
        arr[2] = pause_handle
        arr[3] = speed_handle
        n = 4 if speed_handle else 3
        r = int(kernel32.WaitForMultipleObjects(n, arr, False, INFINITE))
        if r == WAIT_OBJECT_0:
            return 0
        kernel32.CancelWaitableTimer(self._h)
        if r == WAIT_OBJECT_0 + 1:
            return 1
        if r == WAIT_OBJECT_0 + 2:
            return 2
        if n == 4 and r == WAIT_OBJECT_0 + 3:
            return 3
        return 0  # WAIT_FAILED 等异常情况：按到点处理，外层循环会再检查状态

    def _wait_polled(self, seconds: float, stop_handle, pause_handle,
                     speed_handle=None) -> int:
        """拿不到定时器时的兜底：小片轮询，仍保证约 20ms 内响应打断。"""
        import time as _t
        end = _t.perf_counter() + seconds
        while True:
            now = _t.perf_counter()
            if now >= end:
                return 0
            if stop_handle and kernel32.WaitForSingleObject(stop_handle, 0) == WAIT_OBJECT_0:
                return 1
            if pause_handle and kernel32.WaitForSingleObject(pause_handle, 0) == WAIT_OBJECT_0:
                return 2
            if speed_handle and kernel32.WaitForSingleObject(speed_handle, 0) == WAIT_OBJECT_0:
                return 3
            _t.sleep(min(0.02, end - now))

    def close(self) -> None:
        if self._h:
            kernel32.CloseHandle(self._h)
            self._h = None

    def __del__(self) -> None:  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass


def set_dpi_awareness() -> None:
    """必须在创建任何窗口/安装钩子之前调用。

    - DPI 感知后，低级钩子与 GetSystemMetrics 返回真实物理像素，
      与游戏内坐标一致；否则会被系统按缩放虚拟化，回放会偏。
    """
    try:
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        pass


def virtual_screen() -> tuple[int, int, int, int]:
    """返回 (left, top, width, height) 物理像素。

    必须在 set_dpi_awareness() 之后调用才有意义：未声明 DPI 感知时，
    GetSystemMetrics 返回的是被系统按缩放比例虚拟化过的值。
    """
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if width <= 0:
        width = user32.GetSystemMetrics(0)
    if height <= 0:
        height = user32.GetSystemMetrics(1)
    return left, top, width, height


# 屏幕尺寸缓存。绝不能在模块导入时求值——那时 DPI 感知可能还没设置，
# 会缓存下被缩放虚拟化的错误尺寸，导致所有注入坐标系统性偏移。
_VSCREEN: tuple[int, int, int, int] | None = None


def refresh_virtual_screen() -> tuple[int, int, int, int]:
    """重新读取并缓存屏幕尺寸。回放前、录制开始时调用。"""
    global _VSCREEN
    _VSCREEN = virtual_screen()
    return _VSCREEN


def vscreen() -> tuple[int, int, int, int]:
    if _VSCREEN is None:
        return refresh_virtual_screen()
    return _VSCREEN


def to_absolute(x: int, y: int) -> tuple[int, int]:
    """物理像素 -> SendInput 绝对坐标 (0..65535)。"""
    if _VSCREEN is None:
        refresh_virtual_screen()
    left, top, width, height = _VSCREEN
    nx = int(round((x - left) * 65535.0 / max(1, width - 1)))
    ny = int(round((y - top) * 65535.0 / max(1, height - 1)))
    if nx < 0:
        nx = 0
    elif nx > 65535:
        nx = 65535
    if ny < 0:
        ny = 0
    elif ny > 65535:
        ny = 65535
    return nx, ny


def modifier_state() -> int:
    """返回当前按下的修饰键位掩码（用于快捷键匹配）。"""
    mask = 0
    if user32.GetAsyncKeyState(VK_CONTROL) & 0x8000 or user32.GetAsyncKeyState(VK_LCONTROL) & 0x8000 or user32.GetAsyncKeyState(VK_RCONTROL) & 0x8000:
        mask |= 1
    if user32.GetAsyncKeyState(VK_MENU) & 0x8000 or user32.GetAsyncKeyState(VK_LMENU) & 0x8000 or user32.GetAsyncKeyState(VK_RMENU) & 0x8000:
        mask |= 2
    if user32.GetAsyncKeyState(VK_SHIFT) & 0x8000 or user32.GetAsyncKeyState(VK_LSHIFT) & 0x8000 or user32.GetAsyncKeyState(VK_RSHIFT) & 0x8000:
        mask |= 4
    if user32.GetAsyncKeyState(VK_LWIN) & 0x8000 or user32.GetAsyncKeyState(VK_RWIN) & 0x8000:
        mask |= 8
    return mask


def get_async_state(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def is_elevated() -> bool:
    """当前进程是否以管理员身份运行。

    目标程序若以管理员运行，本程序不提升就无法收到它的键鼠事件，
    也无法把输入注入过去（UIPI 限制），因此需要主动检测并提升。
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------- 自我提权
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shell32.ShellExecuteW.argtypes = [w.HWND, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ctypes.c_int]
shell32.ShellExecuteW.restype = ctypes.c_void_p

SW_SHOWNORMAL = 1


def relaunch_elevated(extra_args: list[str] | None = None) -> bool:
    """以管理员身份重新启动本进程。

    静默、无感：直接调 ShellExecuteW 的 "runas" 动词拉起自己，
    不会弹出「你要允许此应用对你的设备进行更改吗」以外的额外窗口。
    只有系统 UAC 确认框是绕不过去的（这是 Windows 的安全边界）。

    返回 True 表示已成功拉起新进程（调用方应立即退出自己）。
    返回 False 表示用户拒绝了 UAC 提示，或拉起失败。
    """
    import subprocess
    import sys

    exe = sys.executable
    args = list(extra_args if extra_args is not None else sys.argv[1:])
    if not getattr(sys, "frozen", False):
        # 源码运行：python main.py ... 需要把脚本路径带上
        import os
        script = os.path.abspath(sys.argv[0])
        params = subprocess.list2cmdline([script] + args)
    else:
        params = subprocess.list2cmdline(args)

    ctypes.set_last_error(0)
    # restype 是 c_void_p：返回 NULL 时 ctypes 给 None（按失败处理），
    # 正常返回是大于 32 的 HINSTANCE 值
    r = shell32.ShellExecuteW(None, "runas", exe, params, None, SW_SHOWNORMAL)
    return r is not None and int(r) > 32


user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
user32.GetCursorPos.restype = w.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = w.BOOL


def get_cursor_pos() -> tuple[int, int]:
    pt = w.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def set_cursor_pos(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


# ---------------------------------------------------------------- 注入
_mouse_template = INPUT()
_mouse_template.type = INPUT_MOUSE
_kbd_template = INPUT()
_kbd_template.type = INPUT_KEYBOARD


def make_mouse_input(dx: int, dy: int, data: int, flags: int) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi.dx = dx
    inp.u.mi.dy = dy
    inp.u.mi.mouseData = data
    inp.u.mi.dwFlags = flags
    inp.u.mi.time = 0
    inp.u.mi.dwExtraInfo = 0
    return inp


def make_key_input(vk: int, scan: int, flags: int) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = scan
    inp.u.ki.dwFlags = flags
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = 0
    return inp


def send_inputs(inputs: list[INPUT]) -> int:
    """一次系统调用注入整批事件（批量注入可显著降低开销与时序抖动）。"""
    n = len(inputs)
    if n == 0:
        return 0
    arr = (INPUT * n)(*inputs)
    return int(user32.SendInput(n, arr, ctypes.sizeof(INPUT)))


def send_inputs_array(arr) -> int:
    n = len(arr)
    if n == 0:
        return 0
    return int(user32.SendInput(n, arr, ctypes.sizeof(INPUT)))


def vk_to_scan(vk: int) -> int:
    return int(user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)) & 0xFF


# ---------------------------------------------------------------- Raw Input（相对增量录制）
# 全屏游戏中光标被钉在中心，WH_MOUSE_LL 读到的"光标位置"没有意义；
# 游戏视角读的是鼠标相对增量。Raw Input 是与游戏同一数据源：
# RegisterRawInputDevices(RIDEV_INPUTSINK) 后台收 WM_INPUT，读 RAWMOUSE.lLastX/lLastY。
WM_INPUT = 0x00FF
RIM_INPUT = 0
RIM_INPUTSINK = 1
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIDEV_INPUTSINK = 0x00000100
MOUSE_MOVE_ABSOLUTE = 0x0001  # usFlags 置位表示绝对设备（触屏/RDP），不进相对流

HWND_MESSAGE = ctypes.c_void_p(-3)


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", w.USHORT),
        ("usUsage", w.USHORT),
        ("dwFlags", w.DWORD),
        ("hwndTarget", w.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", w.DWORD),
        ("dwSize", w.DWORD),
        ("hDevice", w.HANDLE),
        ("wParam", ctypes.c_size_t),
    ]


class _RAWMOUSE_BTN(ctypes.Structure):
    _fields_ = [("usButtonFlags", w.USHORT), ("usButtonData", w.USHORT)]


class _RAWMOUSE_U(ctypes.Union):
    _fields_ = [("ulButtons", w.ULONG), ("btn", _RAWMOUSE_BTN)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [
        ("usFlags", w.USHORT),
        ("u", _RAWMOUSE_U),
        ("ulRawButtons", w.ULONG),
        ("lLastX", w.LONG),
        ("lLastY", w.LONG),
        ("ulExtraInformation", w.ULONG),
    ]


class RAWINPUT(ctypes.Structure):
    """只声明 mouse 分支：data 联合体各成员偏移相同，够用。"""

    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", w.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", w.HINSTANCE),
        ("hCursor", w.HANDLE),
        ("hbrBackground", w.HANDLE),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
    ]


user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), w.UINT, w.UINT]
user32.RegisterRawInputDevices.restype = w.BOOL
user32.GetRawInputData.argtypes = [w.HANDLE, w.UINT, ctypes.c_void_p,
                                   ctypes.POINTER(w.UINT), w.UINT]
user32.GetRawInputData.restype = w.UINT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = w.ATOM
user32.UnregisterClassW.argtypes = [w.LPCWSTR, w.HINSTANCE]
user32.UnregisterClassW.restype = w.BOOL
user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   w.HWND, w.HANDLE, w.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = w.HWND
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DestroyWindow.argtypes = [w.HWND]
user32.DestroyWindow.restype = w.BOOL
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.GetModuleHandleW.restype = w.HINSTANCE

# 注入事件（SendInput）没有设备句柄，header.hDevice 为 NULL；
# RDP/终端服务下真实事件也可能为 NULL，属已知边缘（见 README）。


def register_raw_mouse(hwnd) -> bool:
    """注册鼠标 Raw Input（后台接收，不抢焦点）。hwnd 为接收 WM_INPUT 的窗口。"""
    dev = RAWINPUTDEVICE(1, 2, RIDEV_INPUTSINK, hwnd)  # usage page 1, usage 2 = 鼠标
    return bool(user32.RegisterRawInputDevices(ctypes.byref(dev), 1, ctypes.sizeof(RAWINPUTDEVICE)))


import itertools as _it

_WINDOW_SEQ = _it.count(1)


def create_message_window(wndproc) -> tuple[int, str] | None:
    """创建 message-only 窗口（不可见、不进任务栏），返回 (hwnd, class_name)。

    每次调用注册**独立**窗口类：WNDPROC 回调绑定在窗口类上，进程内多个
    Recorder 实例并存时（测试/自检会连续创建）必须各自持有自己的回调，
    复用同名类会让 WM_INPUT 全部进第一个实例的回调。
    """
    class_name = f"KMR_RawInput_{next(_WINDOW_SEQ)}"
    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc
    wc.lpszClassName = class_name
    wc.hInstance = kernel32.GetModuleHandleW(None)
    if not user32.RegisterClassW(ctypes.byref(wc)):
        return None
    hwnd = user32.CreateWindowExW(
        0, class_name, None, 0, 0, 0, 0, 0,
        HWND_MESSAGE, None, wc.hInstance, None,
    )
    if not hwnd:
        user32.UnregisterClassW(class_name, wc.hInstance)
        return None
    return hwnd, class_name


def destroy_message_window(hwnd, class_name: str) -> None:
    """销毁窗口并反注册窗口类（须在创建它的线程上调用）。"""
    if hwnd:
        user32.DestroyWindow(hwnd)
    if class_name:
        user32.UnregisterClassW(class_name, kernel32.GetModuleHandleW(None))


# ---------------------------------------------------------------- Tk 收尾
def cancel_stale_tk_timers(widget) -> None:
    """取消 CustomTkinter 遗留的 after 定时器。

    CustomTkinter 排了若干延时回调，但 destroy() 里没有取消：
      - after(200, _windows_set_titlebar_icon)
      - after(1000, _set_scaled_min_max)
      - scaling_tracker 的周期性 check_dpi_scaling
      - appearance_mode_tracker 的 30ms 自轮询 update
      - CTkTextbox 的 _check_if_scrollbars_needed 自轮询（<lambda>）
    窗口销毁后它们照常被 Tcl 触发，就会往 stderr 刷
    `invalid command name "..._windows_set_titlebar_icon"` / `...<lambda>`
    之类的内容。关窗和自检里创建临时窗口时都该调用一次。

    只精确取消这几个已知回调，其余内部定时器保持不动，
    避免破坏 CustomTkinter 自身的清理流程。
    """
    markers = ("set_titlebar_icon", "check_dpi_scaling", "set_scaled_min_max", "update")
    try:
        ids = widget.tk.eval("after info").split()
    except Exception:
        return
    for aid in ids:
        try:
            script = widget.tk.eval(f"after info {aid}")
        except Exception:
            continue
        if any(m in script for m in markers):
            try:
                widget.after_cancel(aid)
            except Exception:
                pass
    # appearance_mode_tracker 把根窗口记在类级 app_list 里，且用类级标志位
    # 控制轮询是否在跑。窗口销毁后必须清掉，否则该标志位会一直是 True，
    # 导致之后新建的窗口再也启动不了外观轮询。
    try:
        from customtkinter.windows.widgets.appearance_mode.appearance_mode_tracker import (
            AppearanceModeTracker as _AMT,
        )
        _AMT.app_list = [a for a in _AMT.app_list if a is not widget]
        if not _AMT.app_list:
            _AMT.update_loop_running = False
    except Exception:
        pass


# ---------------------------------------------------------------- 计时器精度
_timer_depth = 0


def begin_timer_precision() -> None:
    """把系统计时器精度提升到 1ms，让 time.sleep 更准；退出时务必还原。"""
    global _timer_depth
    if winmm is None:
        return
    if _timer_depth == 0:
        try:
            winmm.timeBeginPeriod(1)
        except Exception:
            pass
    _timer_depth += 1


def end_timer_precision() -> None:
    global _timer_depth
    if winmm is None or _timer_depth == 0:
        return
    _timer_depth -= 1
    if _timer_depth == 0:
        try:
            winmm.timeEndPeriod(1)
        except Exception:
            pass
