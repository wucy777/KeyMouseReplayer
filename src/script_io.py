"""脚本序列化：紧凑二进制格式 + 可选 zlib 压缩。

文件格式 (KMS1)
    0   : magic  b"KMS1"
    4   : u16    format version
    6   : u16    header flags (bit0 = zlib 压缩)
    8   : u32    event count
    12  : i32    virtual left
    16  : i32    virtual top
    20  : i32    virtual width
    24  : i32    virtual height
    28  : u32    payload raw byte length
    32  : u32    payload stored byte length
    36  : u32    mouse_mode（v1 为保留字段恒 0：0=绝对坐标 1=相对增量）
    40  : payload

payload: count 条 24 字节记录（小端）
    i32 kind
    i32 a     (按键=虚拟键码；鼠标键=按钮号；滚轮=增量；移动=X 坐标；相对移动=dx)
    i32 b     (按键=扫描码；鼠标键/滚轮=X 坐标；移动=Y 坐标；相对移动=dy)
    i32 c     (按键=extended 标志；鼠标键/滚轮=Y 坐标；移动=0)
    i32 dt_us 距上一条的微秒数（i32 上限约 35.8 分钟，录制侧超 30 分钟按 30 分钟记）
    i32 flags 预留

两种鼠标模式（v2 引入，事件 kind 2/7 互斥出现）：
    绝对坐标（0）：普通程序。移动/点击按物理像素坐标注入，回放中手碰鼠标
                   会被下一条事件自动拉回。
    相对增量（1）：光标被游戏捕获/隐藏的全屏游戏。移动按 SendInput 相对增量
                   注入，点击/滚轮不携带移动标志（落在当前光标处=镜头处）。

单条 24 字节 + 表头，10 万条约 2.4MB，压缩后通常 <1MB。
写入走 array.tobytes，读取走 frombytes，无 Python 循环，毫秒级完成。
"""
from __future__ import annotations

import array
import os
import struct
import zlib
from dataclasses import dataclass, field

MAGIC = b"KMS1"
FORMAT_VERSION = 2
FLAG_ZLIB = 0x0001
HEADER_SIZE = 40
STRIDE = 6
DEFAULT_EXT = ".kms"

MOUSE_MODE_ABS = 0
MOUSE_MODE_REL = 1

# 载入时的事件条数上限（高于录制侧 MAX_EVENTS，留余量）：防止损坏/恶意文件
# 用超大 count 或 zlib 炸弹把内存吃光
MAX_LOAD_EVENTS = 20_000_000

# 大于约 130 万条事件（32MB 原始负载）时改用 zlib level 1：
# 压缩时间从十几秒降到两三秒，这类 dt 密集数据的压缩比几乎不受影响
FAST_COMPRESS_BEYOND = 32_000_000

_HEADER = struct.Struct("<4sHHiiiiIIII")


@dataclass
class Script:
    """一份记录。events 为 array('i')，长度 = count * 6。"""

    events: array.array = field(default_factory=lambda: array.array("i"))
    count: int = 0
    screen: tuple[int, int, int, int] = (0, 0, 1920, 1080)
    name: str = ""
    source: str = ""  # 文件路径，空表示仅内存中
    mouse_mode: int = MOUSE_MODE_ABS

    @property
    def duration_us(self) -> int:
        total = 0
        ev = self.events
        for i in range(4, self.count * STRIDE, STRIDE):
            total += ev[i]
        return total

    @property
    def memory_bytes(self) -> int:
        return self.count * STRIDE * 4


def serialize(script: Script, compress: bool = True) -> bytes:
    n = script.count * STRIDE
    # 长度正好时直接 tobytes，省掉一次临时切片拷贝（百万条级时省几百 MB 峰值）
    raw = script.events.tobytes() if len(script.events) == n else script.events[:n].tobytes()
    flags = 0
    payload = raw
    if compress and raw:
        level = 6 if len(raw) <= FAST_COMPRESS_BEYOND else 1
        comp = zlib.compress(raw, level)
        if len(comp) < len(raw):
            payload = comp
            flags |= FLAG_ZLIB
    left, top, width, height = script.screen
    header = _HEADER.pack(
        MAGIC, FORMAT_VERSION, flags, script.count,
        left, top, width, height,
        len(raw), len(payload), script.mouse_mode,
    )
    return header + payload


def deserialize(data: bytes) -> Script:
    if len(data) < HEADER_SIZE:
        raise ValueError("文件太小，不是有效的脚本文件")
    magic, ver, flags, count, left, top, width, height, raw_len, stored_len, mouse_mode = _HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise ValueError("文件头不是 KMS1，可能是旧版或损坏的脚本")
    if ver not in (1, FORMAT_VERSION):
        raise ValueError(f"脚本格式版本 {ver} 与当前程序 {FORMAT_VERSION} 不兼容")
    if mouse_mode not in (MOUSE_MODE_ABS, MOUSE_MODE_REL):
        raise ValueError("鼠标模式字段异常，文件可能已损坏")
    if count > MAX_LOAD_EVENTS:
        raise ValueError("事件条数异常，文件可能已损坏")
    expected = count * STRIDE * 4
    if raw_len != expected:
        raise ValueError("文件头与负载数据不符，文件可能已损坏")
    payload = data[HEADER_SIZE: HEADER_SIZE + stored_len]
    if flags & FLAG_ZLIB:
        # max_length 限定解压输出上限：损坏/恶意构造的压缩包不会撑爆内存
        d = zlib.decompressobj()
        raw = d.decompress(payload, expected + 1)
        if len(raw) != expected:
            raise ValueError("解压后数据大小与文件头不符")
    else:
        raw = payload
        if len(raw) != expected:
            raise ValueError("脚本内容不完整")
    events = array.array("i")
    events.frombytes(raw)
    return Script(events=events, count=count, screen=(left, top, width, height),
                  mouse_mode=mouse_mode)


def save_file(path: str, script: Script, compress: bool = True) -> int:
    blob = serialize(script, compress=compress)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return len(blob)


def load_file(path: str) -> Script:
    with open(path, "rb") as fh:
        data = fh.read()
    s = deserialize(data)
    s.source = path
    s.name = os.path.splitext(os.path.basename(path))[0]
    return s
