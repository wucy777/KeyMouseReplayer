"""脚本分析 / 预处理：统计、压缩展开、时间轴重建。

面向性能：全部在 array('i') 上做原地操作，不产生 Python 对象列表。
"""
from __future__ import annotations

import array

from . import winapi as W
from .script_io import STRIDE, Script

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
