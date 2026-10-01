"""脚本分析 / 预处理：统计、模式判定、双轨裁剪、时间轴重建。

面向性能：全部在 array('i') 上做原地操作，不产生 Python 对象列表。
"""
from __future__ import annotations

import array

from . import winapi as W
from .script_io import MOUSE_MODE_ABS, MOUSE_MODE_REL, STRIDE, Script

# 预处理策略：把「鼠标停在原地时的时间」折算成移动事件之间的间隙。
# 人类的鼠标移动轨迹在一个点上重复出现多次，逐条下发没有意义，
# 但直接丢弃会丢失停顿节奏，因此合并为：
#   - 连续落在同一点的移动事件，只保留最后一条，其 dt 累加
# 静态按键/点击事件一条不删，保证点击间隔精确。


def merge_stationary_moves(events: array.array, count: int, distance: int = 0) -> tuple[array.array, int]:
    """把连续同坐标的鼠标移动事件合并，dt 累加到保留的那一条上。

    返回新的 (events, count)。distance=0 表示坐标完全相同才合并。
    """
    if count == 0:
        return events, 0
    out = array.array("i")
    out_append = out.append
    ev = events
    i = 0
    n = count
    while i < n:
        kind = ev[i * STRIDE]
        if kind != W.K_MOUSE_MOVE or i + 1 >= n or ev[(i + 1) * STRIDE] != W.K_MOUSE_MOVE:
            out_append(ev[i * STRIDE]); out_append(ev[i * STRIDE + 1]); out_append(ev[i * STRIDE + 2])
            out_append(ev[i * STRIDE + 3]); out_append(ev[i * STRIDE + 4]); out_append(ev[i * STRIDE + 5])
            i += 1
            continue
        # 扫描同坐标连续段（移动事件布局：a=x, b=y）
        x0 = ev[i * STRIDE + 1]
        y0 = ev[i * STRIDE + 2]
        x = x0
        y = y0
        acc = ev[i * STRIDE + 4]
        j = i + 1
        while j < n and ev[j * STRIDE] == W.K_MOUSE_MOVE:
            if abs(ev[j * STRIDE + 1] - x0) > distance or abs(ev[j * STRIDE + 2] - y0) > distance:
                break
            acc += ev[j * STRIDE + 4]
            x = ev[j * STRIDE + 1]
            y = ev[j * STRIDE + 2]
            j += 1
        out_append(W.K_MOUSE_MOVE); out_append(x); out_append(y); out_append(0)
        out_append(acc); out_append(0)
        i = j
    del ev
    return out, len(out) // STRIDE


class Stats:
    __slots__ = (
        "count", "duration_us", "key_down", "key_up", "moves", "clicks",
        "button_up", "wheel", "unique_keys", "memory_bytes", "max_gap_us",
    )

    def __init__(self) -> None:
        self.count = 0
        self.duration_us = 0
        self.key_down = 0
        self.key_up = 0
        self.moves = 0
        self.clicks = 0
        self.button_up = 0
        self.wheel = 0
        self.unique_keys = 0
        self.memory_bytes = 0
        self.max_gap_us = 0


def analyze(script: Script) -> Stats:
    s = Stats()
    ev = script.events
    n = script.count
    s.count = n
    s.memory_bytes = n * STRIDE * 4
    keys = set()
    total = 0
    max_gap = 0
    for i in range(n):
        base = i * STRIDE
        kind = ev[base]
        if kind == W.K_KEY_DOWN:
            s.key_down += 1
            keys.add(ev[base + 1])
        elif kind == W.K_KEY_UP:
            s.key_up += 1
            keys.add(ev[base + 1])
        elif kind == W.K_MOUSE_MOVE:
            s.moves += 1
        elif kind == W.K_MOUSE_REL:
            s.moves += 1
        elif kind == W.K_BUTTON_DOWN:
            # clicks 只按「按下」计：界面上的「点击」一次就是 1
            s.clicks += 1
        elif kind == W.K_BUTTON_UP:
            s.button_up += 1
        elif kind in (W.K_WHEEL, W.K_HWHEEL):
            s.wheel += 1
        dt = ev[base + 4]
        total += dt
        if dt > max_gap:
            max_gap = dt
    s.duration_us = total
    s.unique_keys = len(keys)
    s.max_gap_us = max_gap
    return s


def format_duration(us: int) -> str:
    if us < 0:
        us = 0
    total_ms = us // 1000
    ms = total_ms % 1000
    sec = (total_ms // 1000) % 60
    minute = (total_ms // 60000) % 60
    hour = total_ms // 3600000
    if hour:
        return f"{hour}:{minute:02d}:{sec:02d}.{ms:03d}"
    return f"{minute:02d}:{sec:02d}.{ms:03d}"


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.2f} MB"


# ---------------------------------------------------------------- 双轨判定与裁剪
# 「自动检测」录制会把绝对坐标（kind2）与相对增量（kind7）两路都写进同一条
# 时间轴，存盘时据此判定脚本该用哪种模式，并裁掉另一路。
# 按固定事件数分窗判定：混录「桌面段 + 进游戏段」时，桌面段两路 1:1 不会误触发，
# 游戏段「光标被钉在中心附近而增量巨大」在任意位置都能被识别。
_DETECT_WINDOW = 200          # 判定窗口的事件数
_DETECT_MIN_REL_DISP = 200    # 窗口内相对位移下限（px）
_DETECT_RATIO = 8.0           # 相对位移 / 光标位移 的捕获判定比
_DETECT_MAX_PATH = 64         # 窗口内光标位移上限：被游戏钉住的 signature


def detect_mouse_mode(events: array.array, count: int) -> tuple[int, dict]:
    """判定双轨脚本应为绝对坐标还是相对增量模式。

    返回 (mode, info)。info 供 UI 提示：pinned_light=True 表示
    「绝对模式被选中，但光标几乎没动过」——通常意味着处在捕获鼠标的游戏里
    而 Raw Input 不可用，属于静默失败的兜底提示。
    """
    ev = events
    rel_disp = 0
    abs_moves = 0
    rel_moves = 0
    win_rel = 0
    win_path = 0.0
    px = py = None
    captured = False
    in_window = 0
    for i in range(count):
        base = i * STRIDE
        kind = ev[base]
        if kind == W.K_MOUSE_MOVE:
            x, y = ev[base + 1], ev[base + 2]
            if px is not None:
                win_path += abs(x - px) + abs(y - py)
            px, py = x, y
            abs_moves += 1
        elif kind == W.K_MOUSE_REL:
            dx, dy = ev[base + 1], ev[base + 2]
            rel_disp += abs(dx) + abs(dy)
            win_rel += abs(dx) + abs(dy)
            rel_moves += 1
        else:
            continue  # 非移动事件不影响判定窗口
        in_window += 1
        if in_window >= _DETECT_WINDOW:
            if (not captured and win_rel >= _DETECT_MIN_REL_DISP
                    and win_rel >= _DETECT_RATIO * win_path and win_path <= _DETECT_MAX_PATH):
                captured = True
            in_window = 0
            win_rel = 0
            win_path = 0.0
            px = py = None
    if (not captured and win_rel >= _DETECT_MIN_REL_DISP
            and win_rel >= _DETECT_RATIO * win_path and win_path <= _DETECT_MAX_PATH):
        captured = True
    mode = MOUSE_MODE_REL if captured else MOUSE_MODE_ABS
    info = {
        "rel_disp": rel_disp,
        "abs_moves": abs_moves,
        "rel_moves": rel_moves,
        "pinned_light": mode == MOUSE_MODE_ABS and abs_moves >= 100 and rel_moves == 0,
    }
    return mode, info


def strip_to_mode(events: array.array, count: int, mode: int) -> tuple[array.array, int]:
    """裁掉与目标模式不符的移动事件，时间轴保持精确。

    被裁事件的 dt 并入下一条保留事件的 dt——总时长与各保留事件的绝对时刻
    完全不变。按键/点击/滚轮永远保留。
    """
    if count == 0:
        return events, 0
    drop_kind = W.K_MOUSE_REL if mode == MOUSE_MODE_ABS else W.K_MOUSE_MOVE
    ev = events
    out = array.array("i")
    out_append = out.append
    pending_dt = 0
    kept = 0
    for i in range(count):
        base = i * STRIDE
        kind = ev[base]
        if kind == drop_kind:
            pending_dt += ev[base + 4]
            continue
        out_append(kind)
        out_append(ev[base + 1])
        out_append(ev[base + 2])
        out_append(ev[base + 3])
        out_append(ev[base + 4] + pending_dt)
        out_append(ev[base + 5])
        pending_dt = 0
        kept += 1
    return out, kept
