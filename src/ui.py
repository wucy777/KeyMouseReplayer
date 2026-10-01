"""现代化 UI（CustomTkinter）。

线程模型：
- 主线程：Tk 事件循环 + 每 25ms 一次的任务队列泵（唯一碰控件的地方）。
- input-hook 线程：键盘钩子，负责任期快捷键 → 只往队列塞命令，绝不碰控件。
- player 线程：回放，进度 → 只往队列塞进度。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

from . import analysis, dialogs, script_io, winapi as W
from .player import Player
from .recorder import Recorder
from .script_io import Script
from .ui_theme import (
    ACCENT, ACCENT_HOVER, BG, CARD, CARD2, CARD_HOVER, DANGER, DANGER_HOVER,
    FONT, MONO, MUTED, OK_GREEN, TEXT, WARN,
)
from .version import APP_NAME, __version__


def resource_path(rel: str) -> str:
    """取随程序分发的资源路径。

    PyInstaller 单文件模式会把资源解到临时目录（sys._MEIPASS），
    打包后必须从这里找，否则图标读不到。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, rel)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel)


SPEED_PRESETS = [0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0]

VK_CHOICES = {
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "F11": 0x7A, "F12": 0x7B, "Insert": 0x2D, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22, "Pause": 0x13, "ScrollLock": 0x91,
}


class HotkeyCfg:
    __slots__ = ("key", "vk", "ctrl", "alt", "shift")

    def __init__(self, key: str = "F9", ctrl=False, alt=False, shift=False):
        self.key = key
        self.vk = VK_CHOICES.get(key, 0x78)
        self.ctrl = ctrl
        self.alt = alt
        self.shift = shift

    @property
    def mask(self) -> int:
        m = 0
        if self.ctrl:
            m |= 1
        if self.alt:
            m |= 2
        if self.shift:
            m |= 4
        return m

    def label(self) -> str:
        parts = []
        if self.ctrl:
            parts.append("Ctrl")
        if self.alt:
            parts.append("Alt")
        if self.shift:
            parts.append("Shift")
        parts.append(self.key)
        return "+".join(parts)


class App(ctk.CTk):
    def __init__(self, elevated: bool | None = None):
        super().__init__()
        self.title(f"{APP_NAME}  v{__version__}")
        self.geometry("1060x700")
        self.minsize(940, 640)
        self.configure(fg_color=BG)

        # 由入口传入（入口在建窗口前就已确保提权）；缺省时现场检测
        self.elevated = W.is_elevated() if elevated is None else bool(elevated)

        self.hk_record = HotkeyCfg("F9")
        self.hk_play = HotkeyCfg("F10")
        self.hk_pause = HotkeyCfg("F11")
        self.hk_stop = HotkeyCfg("F12")

        self.script: Script | None = None
        self.script_name = "未载入"
        self.move_min_interval = 8
        self.move_min_distance = 2
        self.compress_on_save = True
        self.last_dir = os.path.join(os.path.expanduser("~"), "Documents")

        self._q: queue.Queue = queue.Queue(maxsize=4096)
        self._rec_start = 0.0
        self._last_executed = 0
        self._list_shown = 0
        self._pending_ui: list = []
        self._after_ids: list = []          # 一次性定时回调
        self._recurring_aids: dict[str, int] = {}  # 周期性定时回调（覆盖式登记）
        self._closing = False

        self.recorder = Recorder()
        self.recorder.start()
        self.player = Player()
        self.player.on_progress = self._on_progress
        self.player.on_finish = self._on_finish
        self._install_hotkeys()

        self._build()
        self._apply_icon()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after(self._pump, 25)
        self._after(self._tick, 100)
        self._after(self._startup_check, 400)

    def _apply_icon(self) -> None:
        """设置窗口与任务栏图标；失败不影响使用。"""
        ico = resource_path(os.path.join("assets", "app.ico"))
        if not os.path.exists(ico):
            return
        # 先声明已设置图标，避免 CustomTkinter 的延时回调又把默认图标盖回去
        try:
            self._iconbitmap_method_called = True
        except Exception:
            pass
        try:
            self.iconbitmap(ico)
        except Exception:
            pass

    def _after(self, fn, ms: int, recurring: bool = False) -> None:
        """登记自排定时器，退出时统一取消。

        周期性回调（队列泵/时钟）覆盖式登记，避免退出清单随运行时长无限增长。
        """
        if self._closing:
            return
        aid = self.after(ms, fn)
        if recurring:
            self._recurring_aids[fn.__name__] = aid
        else:
            self._after_ids.append(aid)

    def _startup_check(self) -> None:
        """启动自检：钩子是否装上、权限是否够。问题只提示一次，不阻塞使用。"""
        if self._closing:
            return
        msgs = []
        if not self.recorder.kb_hook_ok:
            msgs.append("• 全局键盘钩子安装失败，快捷键和录制都无法工作。\n"
                        "  请尝试以管理员身份运行本程序。")
        if not self.elevated:
            msgs.append("• 当前不是管理员权限（可能是在 UAC 提示中选择了「否」）。\n"
                        "  若目标程序以管理员运行，将收不到它的键鼠事件、也无法把操作回放给它。\n"
                        "  请关闭本程序后重新双击运行，并在系统提示中选择「是」。")
        if msgs:
            dialogs.warning(self, "\n\n".join(msgs), "运行环境提示")

    # ================================================================ 构建
    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ---------------- 侧栏
        side = ctk.CTkFrame(self, width=214, corner_radius=0, fg_color=CARD)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        ctk.CTkLabel(side, text="键鼠录制回放", font=(FONT, 20, "bold"), text_color=TEXT).pack(padx=22, pady=(26, 2), anchor="w")
        ctk.CTkLabel(side, text=f"v{__version__}", font=(FONT, 11), text_color=MUTED).pack(padx=24, anchor="w")

        self.nav_btns = {}
        for key, label in (("record", "  录制"), ("script", "  脚本与回放"), ("settings", "  设置")):
            b = ctk.CTkButton(
                side, text=label, anchor="w", height=42, corner_radius=10,
                font=(FONT, 14), fg_color="transparent", hover_color=CARD2,
                text_color=MUTED, command=lambda k=key: self._show(k),
            )
            b.pack(fill="x", padx=12, pady=3)
            self.nav_btns[key] = b

        self.side_status = ctk.CTkLabel(side, text="● 待机", font=(FONT, 12), text_color=MUTED)
        self.side_status.pack(side="bottom", pady=(0, 6))
        # 权限状态常驻可见：没提权时用户能一眼看出，而不是等到回放失效才困惑
        self.side_priv = ctk.CTkLabel(
            side,
            text=("管理员" if self.elevated else "普通权限"),
            font=(FONT, 11),
            text_color=(MUTED if self.elevated else WARN),
        )
        self.side_priv.pack(side="bottom", pady=(0, 2))

        # ---------------- 内容区
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.grid(row=0, column=1, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)
        wrap.grid_rowconfigure(0, weight=1)
        self.pages = {}
        for key in ("record", "script", "settings"):
            f = ctk.CTkFrame(wrap, fg_color="transparent")
            f.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = f
        self._build_record(self.pages["record"])
        self._build_script(self.pages["script"])
        self._build_settings(self.pages["settings"])
        self._show("record")

    # ---------------------------------------------------------- 录制页
    def _build_record(self, p) -> None:
        p.grid_columnconfigure(0, weight=1)
        p.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(p, text="录制", font=(FONT, 24, "bold"), text_color=TEXT).grid(
            row=0, column=0, sticky="w", padx=28, pady=(24, 8))

        card = ctk.CTkFrame(p, corner_radius=16, fg_color=CARD)
        card.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 24))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)

        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=0, column=0, pady=40)

        self.dot = ctk.CTkLabel(inner, text="●", font=(FONT, 44), text_color=MUTED)
        self.dot.pack()
        self.rec_state = ctk.CTkLabel(inner, text="待机中", font=(FONT, 30, "bold"), text_color=TEXT)
        self.rec_state.pack(pady=(4, 0))
        self.rec_timer = ctk.CTkLabel(inner, text="00:00.000", font=("Consolas", 46, "bold"), text_color=MUTED)
        self.rec_timer.pack(pady=(6, 0))
        self.rec_count = ctk.CTkLabel(inner, text="已记录 0 条操作", font=(FONT, 14), text_color=MUTED)
        self.rec_count.pack(pady=(8, 0))

        self.rec_btn = ctk.CTkButton(
            inner, text=f"开始录制  ({self.hk_record.label()})", width=290, height=52,
            corner_radius=12, font=(FONT, 16, "bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self.toggle_record)
        self.rec_btn.pack(pady=(30, 0))

        self.rec_hint = ctk.CTkLabel(
            inner, text="按下快捷键开始 / 结束录制，录制期间本窗口可最小化",
            font=(FONT, 12), text_color=MUTED)
        self.rec_hint.pack(pady=(16, 0))

    # ---------------------------------------------------------- 脚本页
    def _build_script(self, p) -> None:
        p.grid_columnconfigure(0, weight=1)
        p.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(p, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 6))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="脚本与回放", font=(FONT, 24, "bold"), text_color=TEXT).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(head, text="打开脚本…", width=110, height=34, corner_radius=9, font=(FONT, 13),
                      fg_color=CARD2, hover_color="#2C3140", command=self.open_script).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(head, text="保存为文件…", width=120, height=34, corner_radius=9, font=(FONT, 13),
                      fg_color=CARD2, hover_color="#2C3140", command=self.save_script).grid(row=0, column=2)

        # 信息条
        info = ctk.CTkFrame(p, corner_radius=14, fg_color=CARD)
        info.grid(row=1, column=0, sticky="ew", padx=28, pady=(6, 10))
        for i in range(6):
            info.grid_columnconfigure(i, weight=1)
        self.info_labels = {}
        for i, (k, t) in enumerate((("name", "脚本"), ("count", "操作数"), ("dur", "总时长"),
                                    ("size", "内存占用"), ("click", "点击"), ("key", "按键"))):
            cell = ctk.CTkFrame(info, fg_color="transparent")
            cell.grid(row=0, column=i, sticky="ew", padx=12, pady=12)
            ctk.CTkLabel(cell, text=t, font=(FONT, 11), text_color=MUTED).pack(anchor="w")
            v = ctk.CTkLabel(cell, text="—", font=(FONT, 15, "bold"), text_color=TEXT)
            v.pack(anchor="w")
            self.info_labels[k] = v

        # 控制区
        ctrl = ctk.CTkFrame(p, corner_radius=14, fg_color=CARD)
        ctrl.grid(row=2, column=0, sticky="nsew", padx=28, pady=(0, 24))
        ctrl.grid_columnconfigure(0, weight=1)
        ctrl.grid_rowconfigure(4, weight=1)

        row = ctk.CTkFrame(ctrl, fg_color="transparent")
        row.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 6))
        self.btn_play = ctk.CTkButton(row, text=f"▶  播放  ({self.hk_play.label()})", width=170, height=44,
                                      corner_radius=11, font=(FONT, 15, "bold"), fg_color=ACCENT,
                                      hover_color=ACCENT_HOVER, command=self.play_from_start)
        self.btn_play.pack(side="left")
        self.btn_pause = ctk.CTkButton(row, text=f"⏸  暂停  ({self.hk_pause.label()})", width=150, height=44,
                                       corner_radius=11, font=(FONT, 14), fg_color=CARD2,
                                       hover_color="#2C3140", command=self.toggle_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=8)
        self.btn_stop = ctk.CTkButton(row, text=f"⏹  停止  ({self.hk_stop.label()})", width=150, height=44,
                                      corner_radius=11, font=(FONT, 14), fg_color=CARD2,
                                      hover_color="#2C3140", command=self.stop_play, state="disabled")
        self.btn_stop.pack(side="left")
        self.btn_resume = ctk.CTkButton(row, text="↻ 从已执行位置继续", width=180, height=44, corner_radius=11,
                                        font=(FONT, 14), fg_color=CARD2, hover_color="#2C3140",
                                        command=self.play_from_position)
        self.btn_resume.pack(side="right")

        # 进度
        prog = ctk.CTkFrame(ctrl, fg_color="transparent")
        prog.grid(row=1, column=0, sticky="ew", padx=18, pady=(6, 2))
        prog.grid_columnconfigure(0, weight=1)
        self.bar = ctk.CTkProgressBar(prog, height=10, corner_radius=5, progress_color=ACCENT)
        self.bar.grid(row=0, column=0, sticky="ew")
        self.bar.set(0)
        self.lbl_prog = ctk.CTkLabel(prog, text="0 / 0", font=("Consolas", 12), text_color=MUTED, width=170)
        self.lbl_prog.grid(row=0, column=1, padx=(10, 0))
        self.lbl_state = ctk.CTkLabel(prog, text="就绪", font=(FONT, 12), text_color=MUTED, width=110)
        self.lbl_state.grid(row=0, column=2, padx=(6, 0))

        # 倍速 + 起点
        opt = ctk.CTkFrame(ctrl, fg_color="transparent")
        opt.grid(row=2, column=0, sticky="ew", padx=18, pady=(10, 2))
        opt.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(opt, text="延迟倍速", font=(FONT, 13), text_color=TEXT).grid(row=0, column=0, sticky="w")
        self.lbl_speed = ctk.CTkLabel(opt, text="1.00x", font=(FONT, 13, "bold"), text_color=ACCENT, width=58)
        self.lbl_speed.grid(row=0, column=1, padx=(8, 6))
        self.speed_slider = ctk.CTkSlider(opt, from_=0.1, to=8.0, number_of_steps=158,
                                          command=self._on_speed_slide, progress_color=ACCENT)
        self.speed_slider.set(1.0)
        self.speed_slider.grid(row=0, column=2, sticky="ew")
        ctk.CTkLabel(opt, text="1.0x 为原始速度，数值越大播放越快", font=(FONT, 12),
                     text_color=MUTED).grid(row=0, column=3, padx=(12, 0))

        opt2 = ctk.CTkFrame(ctrl, fg_color="transparent")
        opt2.grid(row=3, column=0, sticky="ew", padx=18, pady=(6, 4))
        ctk.CTkLabel(opt2, text="快捷倍速", font=(FONT, 13), text_color=TEXT).pack(side="left", padx=(0, 8))
        for sp in SPEED_PRESETS:
            ctk.CTkButton(opt2, text=f"{sp:g}x", width=48, height=28, corner_radius=8, font=(FONT, 12),
                          fg_color=CARD2, hover_color="#2C3140",
                          command=lambda s=sp: self.set_speed(s)).pack(side="left", padx=2)
        ctk.CTkLabel(opt2, text="起始位置", font=(FONT, 13), text_color=TEXT).pack(side="left", padx=(20, 6))
        self.ent_start = ctk.CTkEntry(opt2, width=86, height=30, font=("Consolas", 12), corner_radius=8,
                                      fg_color=CARD2, border_width=0)
        self.ent_start.insert(0, "0")
        self.ent_start.pack(side="left")
        ctk.CTkLabel(opt2, text="条（0 = 从头播放）", font=(FONT, 12), text_color=MUTED).pack(side="left", padx=(6, 0))

        # 预览
        prev = ctk.CTkFrame(ctrl, fg_color="transparent")
        prev.grid(row=4, column=0, sticky="nsew", padx=18, pady=(10, 16))
        prev.grid_columnconfigure(0, weight=1)
        prev.grid_rowconfigure(1, weight=1)
        prev.grid_columnconfigure(1, weight=0)
        ctk.CTkLabel(prev, text="操作预览（仅显示前 300 条，避免大脚本卡顿）",
                     font=(FONT, 12), text_color=MUTED).grid(row=0, column=0, columnspan=2,
                                                             sticky="w", pady=(0, 4))
        # 用原生 tk.Text 而不是 CTkTextbox：CTkTextbox 内部有一个 200ms 的
        # 滚动条自轮询（用于动态显隐滚动条），脚本预览是只读文本，不需要它，
        # 少一个轮询就少一份开销，也免去它在销毁后触发回调的麻烦。
        box = ctk.CTkFrame(prev, fg_color="#0F1115", corner_radius=10)
        box.grid(row=1, column=0, columnspan=2, sticky="nsew")
        box.grid_columnconfigure(0, weight=1)
        box.grid_rowconfigure(0, weight=1)
        self.preview = tk.Text(
            box, font=(MONO, 12), bg="#0F1115", fg="#C9D1D9", insertbackground="#C9D1D9",
            relief="flat", borderwidth=0, highlightthickness=0, wrap="none",
            padx=8, pady=6, selectbackground=ACCENT, selectforeground="#FFFFFF",
        )
        self.preview.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        sb = ctk.CTkScrollbar(box, command=self.preview.yview,
                              button_color=CARD2, button_hover_color=CARD_HOVER)
        sb.grid(row=0, column=1, sticky="ns", padx=(2, 6), pady=8)
        self.preview.configure(yscrollcommand=sb.set, state="disabled")

    # ---------------------------------------------------------- 设置页
    def _build_settings(self, p) -> None:
        p.grid_columnconfigure(0, weight=1)
        p.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(p, text="设置", font=(FONT, 24, "bold"), text_color=TEXT).grid(
            row=0, column=0, sticky="w", padx=28, pady=(24, 6))

        # 可滚动，窗口再小也不会把内容挤出可见区域
        wrap = ctk.CTkScrollableFrame(p, fg_color="transparent", corner_radius=0,
                                      scrollbar_button_color=CARD2,
                                      scrollbar_button_hover_color="#2C3140")
        wrap.grid(row=1, column=0, sticky="nsew", padx=(20, 12), pady=(0, 16))
        wrap.grid_columnconfigure(0, weight=1)

        card = ctk.CTkFrame(wrap, corner_radius=16, fg_color=CARD)
        card.grid(row=0, column=0, sticky="ew", padx=8)
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text="全局快捷键", font=(FONT, 16, "bold"), text_color=TEXT).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=20, pady=(18, 8))

        self.hk_widgets = {}
        specs = (
            ("record", "开始 / 结束录制", self.hk_record),
            ("play", "播放", self.hk_play),
            ("pause", "暂停 / 继续", self.hk_pause),
            ("stop", "停止回放", self.hk_stop),
        )
        for i, (key, label, cfg) in enumerate(specs):
            r = i + 1
            ctk.CTkLabel(card, text=label, font=(FONT, 13), text_color=TEXT).grid(
                row=r, column=0, sticky="w", padx=20, pady=5)
            var = tk.StringVar(value=cfg.key)
            om = ctk.CTkOptionMenu(card, values=list(VK_CHOICES.keys()), variable=var, width=120, height=30,
                                   font=(FONT, 12), fg_color=CARD2, button_color=CARD2,
                                   button_hover_color="#2C3140", corner_radius=8)
            om.grid(row=r, column=1, sticky="w", pady=5)
            vars_ = []
            for j, (attr, name) in enumerate((("ctrl", "Ctrl"), ("alt", "Alt"), ("shift", "Shift"))):
                v = tk.BooleanVar(value=getattr(cfg, attr))
                ctk.CTkCheckBox(card, text=name, variable=v, font=(FONT, 12), width=70,
                                checkbox_width=18, checkbox_height=18, corner_radius=5,
                                fg_color=ACCENT, hover_color=ACCENT_HOVER).grid(
                    row=r, column=2 + j, sticky="w", padx=(4, 0), pady=5)
                vars_.append((attr, v))
            self.hk_widgets[key] = (om, var, vars_)

        ctk.CTkButton(card, text="应用快捷键", width=120, height=34, corner_radius=9, font=(FONT, 13),
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=self.apply_hotkeys).grid(row=6, column=0, sticky="w", padx=20, pady=(10, 4))

        ctk.CTkLabel(card, text="鼠标移动采样", font=(FONT, 16, "bold"), text_color=TEXT).grid(
            row=7, column=0, columnspan=4, sticky="w", padx=20, pady=(20, 4))
        ctk.CTkLabel(card, text="录制时丢弃过于密集的移动点，是控制事件量、保证回放稳定的关键。\n"
                               "阈值越小越还原，事件数越多；推荐 6~10ms / 2px。",
                     font=(FONT, 12), text_color=MUTED, justify="left").grid(
            row=8, column=0, columnspan=4, sticky="w", padx=20, pady=(0, 6))
        self.lbl_mi = ctk.CTkLabel(card, text="最小间隔  8 ms", font=(FONT, 13), text_color=TEXT, width=150)
        self.lbl_mi.grid(row=9, column=0, sticky="w", padx=20)
        self.sl_mi = ctk.CTkSlider(card, from_=0, to=40, number_of_steps=40, width=300,
                                   progress_color=ACCENT, command=self._on_mi)
        self.sl_mi.set(8)
        self.sl_mi.grid(row=9, column=1, columnspan=3, sticky="w", pady=5)
        self.lbl_md = ctk.CTkLabel(card, text="最小距离  2 px", font=(FONT, 13), text_color=TEXT, width=150)
        self.lbl_md.grid(row=10, column=0, sticky="w", padx=20)
        self.sl_md = ctk.CTkSlider(card, from_=0, to=20, number_of_steps=20, width=300,
                                   progress_color=ACCENT, command=self._on_md)
        self.sl_md.set(2)
        self.sl_md.grid(row=10, column=1, columnspan=3, sticky="w", pady=5)

        ctk.CTkLabel(card, text="脚本文件", font=(FONT, 16, "bold"), text_color=TEXT).grid(
            row=11, column=0, columnspan=4, sticky="w", padx=20, pady=(20, 4))
        self.var_compress = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(card, text="保存时压缩（体积约减小 60~80%，读取速度几乎无差异）",
                        variable=self.var_compress, font=(FONT, 12), checkbox_width=18, checkbox_height=18,
                        corner_radius=5, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        command=self._on_compress).grid(row=12, column=0, columnspan=4, sticky="w",
                                                        padx=20, pady=(0, 4))

        # ---- 运行权限
        ctk.CTkLabel(card, text="运行权限", font=(FONT, 16, "bold"), text_color=TEXT).grid(
            row=13, column=0, columnspan=4, sticky="w", padx=20, pady=(20, 4))
        priv = ctk.CTkFrame(card, fg_color="transparent")
        priv.grid(row=14, column=0, columnspan=4, sticky="w", padx=20, pady=(0, 18))
        self.lbl_priv = ctk.CTkLabel(
            priv,
            text=("已以管理员权限运行" if self.elevated
                  else "当前为普通权限，无法作用于以管理员运行的程序"),
            font=(FONT, 12), text_color=(OK_GREEN if self.elevated else WARN),
            justify="left",
        )
        self.lbl_priv.pack(anchor="w")
        if not self.elevated:
            ctk.CTkButton(priv, text="以管理员身份重新启动", width=170, height=34,
                          corner_radius=9, font=(FONT, 13), fg_color=ACCENT,
                          hover_color=ACCENT_HOVER,
                          command=self.restart_as_admin).pack(anchor="w", pady=(10, 0))

        tip = ctk.CTkFrame(wrap, corner_radius=14, fg_color="#1A1D24")
        tip.grid(row=1, column=0, sticky="ew", padx=8, pady=(14, 8))
        ctk.CTkLabel(tip, text=(
            "使用提示\n"
            "• 录制期间不要用当前窗口本身去点按钮，用快捷键即可，避免把点窗口的动作也录进去。\n"
            "• 回放开始前建议先切到与录制时相同的场景与分辨率，否则坐标、时序会对不上。\n"
            "• 回放中若目标程序卡顿或闪退，按暂停 / 停止干预，处理完后点「从已执行位置继续」接着跑。\n"
            "• 脚本文件可在其他电脑上用本程序打开；坐标按物理像素记录，两端分辨率与系统缩放需保持一致（不一致时程序会提醒）。"
        ), font=(FONT, 12), text_color=MUTED, justify="left").pack(padx=18, pady=14, anchor="w")

    # ================================================================ 导航
    def _show(self, key: str) -> None:
        for k, f in self.pages.items():
            if k == key:
                f.tkraise()
            b = self.nav_btns[k]
            b.configure(fg_color=CARD2 if k == key else "transparent",
                        text_color=TEXT if k == key else MUTED)
        self._page = key

    # ================================================================ 快捷键
    def _install_hotkeys(self) -> None:
        mapping = {
            self.hk_record.vk: (self.hk_record.mask, self._hk_record),
            self.hk_play.vk: (self.hk_play.mask, self._hk_play),
            self.hk_pause.vk: (self.hk_pause.mask, self._hk_pause),
            self.hk_stop.vk: (self.hk_stop.mask, self._hk_stop),
        }
        self.recorder.set_hotkeys(mapping, enabled=True)

    def apply_hotkeys(self) -> None:
        try:
            for key, cfg in (("record", self.hk_record), ("play", self.hk_play),
                             ("pause", self.hk_pause), ("stop", self.hk_stop)):
                om, var, vars_ = self.hk_widgets[key]
                cfg.key = var.get()
                cfg.vk = VK_CHOICES[cfg.key]
                for attr, v in vars_:
                    setattr(cfg, attr, bool(v.get()))
            vks = [self.hk_record.vk, self.hk_play.vk, self.hk_pause.vk, self.hk_stop.vk]
            if len(set(vks)) != len(vks):
                dialogs.warning(self, "四个快捷键不能重复，请重新选择。", "快捷键冲突")
                return
            self._install_hotkeys()
            self.rec_btn.configure(text=f"开始录制  ({self.hk_record.label()})")
            self.btn_play.configure(text=f"▶  播放  ({self.hk_play.label()})")
            self.btn_pause.configure(text=f"⏸  暂停  ({self.hk_pause.label()})")
            self.btn_stop.configure(text=f"⏹  停止  ({self.hk_stop.label()})")
        except Exception as exc:
            dialogs.error(self, str(exc), "应用失败")

    def _hk_record(self) -> None:
        self._q.put(("cmd", "toggle_record"))

    def _hk_play(self) -> None:
        self._q.put(("cmd", "play"))

    def _hk_pause(self) -> None:
        self._q.put(("cmd", "pause"))

    def _hk_stop(self) -> None:
        self._q.put(("cmd", "stop"))

    # ================================================================ 队列泵
    def _pump(self) -> None:
        if self._closing:
            return
        try:
            for _ in range(120):
                item = self._q.get_nowait()
                kind = item[0]
                if kind == "cmd":
                    self._do_cmd(item[1])
                elif kind == "progress":
                    self._render_progress(item[1], item[2])
                elif kind == "finish":
                    self._render_finish(item[1], item[2])
                elif kind == "stats":
                    self._render_stats(item[1], item[2])
        except queue.Empty:
            pass
        except Exception:
            pass
        finally:
            self._after(self._pump, 25, recurring=True)

    def _do_cmd(self, cmd: str) -> None:
        # 模态对话框打开时 wait_window 会继续驱动队列泵，此时执行快捷键命令
        # 会在确认框底下再入（叠对话框/改变状态），直接丢弃，等用户关闭后再按
        if self._closing or dialogs.modal_active():
            return
        if cmd == "toggle_record":
            self.toggle_record()
        elif cmd == "play":
            if self.player.playing:
                self.toggle_pause()
            elif self.bar.get() > 0 and self._last_executed > 0:
                self.play_from_position()
            else:
                self.play_from_start()
        elif cmd == "pause":
            self.toggle_pause()
        elif cmd == "stop":
            self.stop_play()

    # ================================================================ 录制
    def toggle_record(self) -> None:
        if self.recorder.recording:
            self.stop_record()
            return
        if self.player.playing:
            # 录制键在回放中只负责停止回放：一边停一边立刻开始录，
            # 容易让用户分不清当前处于什么状态
            self.stop_play()
            self._show("record")
            self.rec_hint.configure(text="已停止回放；再按一次开始录制")
            return
        self.start_record()

    def start_record(self) -> None:
        if self.player.playing:
            dialogs.info(self, "请先停止回放再开始录制。")
            return
        self.recorder.move_min_interval_us = self.move_min_interval * 1000
        self.recorder.move_min_distance = self.move_min_distance
        try:
            self.recorder.start_recording()
        except OSError as exc:
            dialogs.error(self, str(exc), "无法录制")
            return
        self._rec_start = time.perf_counter()
        self.dot.configure(text_color=DANGER)
        self.rec_state.configure(text="录制中", text_color=DANGER)
        self.rec_btn.configure(text=f"结束录制  ({self.hk_record.label()})", fg_color=DANGER, hover_color=DANGER_HOVER)
        self.rec_hint.configure(text="正在记录键鼠操作…  再次按下快捷键结束")
        self.side_status.configure(text="● 录制中", text_color=DANGER)
        self._show("record")

    def stop_record(self) -> None:
        self.recorder.stop_recording()
        self.rec_btn.configure(text=f"开始录制  ({self.hk_record.label()})", fg_color=ACCENT, hover_color=ACCENT_HOVER)
        self.dot.configure(text_color=OK_GREEN)
        self.rec_state.configure(text="录制完成", text_color=TEXT)
        self.rec_hint.configure(text="已存入程序内存，可在「脚本与回放」页面回放或保存为文件")
        self.side_status.configure(text="● 待机", text_color=MUTED)

        buf, cnt = self.recorder.take_events()
        if cnt == 0:
            dialogs.info(self, "本次没有记录到任何操作。")
            return
        scr = Script(events=buf, count=cnt, screen=W.refresh_virtual_screen(), name="")
        ts = time.strftime("%Y%m%d-%H%M%S")
        scr.name = f"录制-{ts}"
        self._set_script(scr)
        self._show("script")
        if self.recorder.overflowed:
            dialogs.warning(self, "事件数达到保护上限，后续操作未被记录。", "达到上限")

    # ================================================================ 脚本
    def _set_script(self, scr: Script) -> None:
        self.script = scr
        self.script_name = scr.name or "未命名"
        self._last_executed = 0
        self.info_labels["name"].configure(text=self._trunc(self.script_name, 14))
        self.info_labels["count"].configure(text=f"{scr.count:,}")
        for k in ("dur", "size", "click", "key"):
            self.info_labels[k].configure(text="…")
        self.bar.set(0)
        self.lbl_prog.configure(text=f"0 / {scr.count:,}")
        self.ent_start.delete(0, "end")
        self.ent_start.insert(0, "0")
        self._fill_preview()
        # 大脚本（百万条级）的统计是秒级 Python 循环，放后台线程算，
        # 结果经队列回主线程刷新；期间用户照常可以立即播放
        threading.Thread(target=self._analyze_bg, args=(scr,), name="analyze", daemon=True).start()

    def _analyze_bg(self, scr: Script) -> None:
        try:
            st = analysis.analyze(scr)
        except Exception:
            st = None
        try:
            self._q.put_nowait(("stats", st, scr))
        except queue.Full:
            pass

    def _render_stats(self, st, scr: Script) -> None:
        if scr is not self.script:
            return  # 统计算完前用户又载入了别的脚本，结果作废
        if st is None:
            for k in ("dur", "size", "click", "key"):
                self.info_labels[k].configure(text="—")
            return
        self.info_labels["dur"].configure(text=analysis.format_duration(st.duration_us))
        self.info_labels["size"].configure(text=analysis.format_size(st.memory_bytes))
        self.info_labels["click"].configure(text=f"{st.clicks:,}")
        self.info_labels["key"].configure(text=f"{st.key_down:,}")

    def _fill_preview(self) -> None:
        scr = self.script
        if scr is None:
            return
        ev = scr.events
        lines = []
        t = 0
        limit = min(300, scr.count)
        for i in range(limit):
            base = i * 6
            kind = ev[base]
            t += ev[base + 4]
            lines.append(f"{i + 1:>6}  {analysis.format_duration(t):>10}  {self._describe(ev, base)}")
        if scr.count > limit:
            lines.append(f"…… 其余 {scr.count - limit:,} 条已省略")
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", "\n".join(lines))
        self.preview.configure(state="disabled")

    @staticmethod
    def _describe(ev, base: int) -> str:
        kind = ev[base]
        a = ev[base + 1]
        if kind == W.K_MOUSE_MOVE:
            return f"鼠标移动  ->  ({a}, {ev[base + 2]})"
        if kind == W.K_BUTTON_DOWN:
            return f"{W.BTN_NAMES.get(a, a)} 按下  @({ev[base + 2]}, {ev[base + 3]})"
        if kind == W.K_BUTTON_UP:
            return f"{W.BTN_NAMES.get(a, a)} 抬起  @({ev[base + 2]}, {ev[base + 3]})"
        if kind == W.K_WHEEL:
            return f"滚轮  {'上' if a > 0 else '下'}  {abs(a) // 120} 格  @({ev[base + 2]}, {ev[base + 3]})"
        if kind == W.K_HWHEEL:
            return f"横向滚轮  {a}  @({ev[base + 2]}, {ev[base + 3]})"
        if kind == W.K_KEY_DOWN:
            return f"按键  {vk_name(a)} 按下"
        if kind == W.K_KEY_UP:
            return f"按键  {vk_name(a)} 抬起"
        return f"未知事件 {kind}"

    @staticmethod
    def _trunc(s: str, n: int) -> str:
        return s if len(s) <= n else s[: n - 1] + "…"

    def open_script(self) -> None:
        path = filedialog.askopenfilename(
            title="打开脚本",
            initialdir=self.last_dir,
            filetypes=[("键鼠脚本", "*.kms"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            scr = script_io.load_file(path)
        except Exception as exc:
            dialogs.error(self, str(exc), "打开失败")
            return
        self.last_dir = os.path.dirname(path)
        self._set_script(scr)
        self.lbl_state.configure(text="已载入", text_color=OK_GREEN)

    def save_script(self) -> None:
        if self.script is None or self.script.count == 0:
            dialogs.info(self, "当前没有可保存的脚本。")
            return
        path = filedialog.asksaveasfilename(
            title="保存脚本",
            initialdir=self.last_dir,
            initialfile=f"{self.script_name}{script_io.DEFAULT_EXT}",
            defaultextension=script_io.DEFAULT_EXT,
            filetypes=[("键鼠脚本", "*.kms"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            size = script_io.save_file(path, self.script, compress=self.compress_on_save)
        except Exception as exc:
            dialogs.error(self, str(exc), "保存失败")
            return
        self.last_dir = os.path.dirname(path)
        self.script.source = path
        dialogs.success(self, f"已保存：\n{path}\n\n文件大小 {analysis.format_size(size)}", "保存成功")

    # ================================================================ 回放
    def _read_speed(self) -> float:
        return float(self.speed_slider.get())

    def _on_speed_slide(self, value) -> None:
        self.lbl_speed.configure(text=f"{float(value):.2f}x")
        self.player.speed = float(value)
        self.player.notify_speed_change()  # 等待中的回放立即按新倍速重算时刻

    def set_speed(self, sp: float) -> None:
        self.speed_slider.set(sp)
        self._on_speed_slide(sp)

    def _read_start(self) -> int:
        try:
            return max(0, int(float(self.ent_start.get())))
        except Exception:
            return 0

    def play_from_start(self) -> None:
        if self.script is None or self.script.count == 0:
            dialogs.info(self, "请先录制或打开一个脚本。")
            return
        if self.player.playing:
            return
        start = self._read_start()
        if start >= self.script.count:
            start = 0
        self._start_play(start)

    def play_from_position(self) -> None:
        if self.script is None or self.script.count == 0:
            dialogs.info(self, "请先录制或打开一个脚本。")
            return
        if self.player.playing:
            return
        idx = self.player.executed or self._last_executed
        if idx >= self.script.count:
            idx = 0  # 已执行到最后：静默从头重放，重复任务场景不必每次确认
        self.ent_start.delete(0, "end")
        self.ent_start.insert(0, str(idx))
        self._start_play(idx)

    def _start_play(self, start: int) -> None:
        if self.script is None:
            return
        cur = W.refresh_virtual_screen()
        if self.script.screen != cur and self.script.screen[2] > 0:
            sl, st, sw, sh = self.script.screen
            cl, ct, cw, ch = cur
            if not dialogs.confirm(
                self,
                f"录制时的显示区域与当前不同：\n\n"
                f"  录制时  {sw} × {sh}  (偏移 {sl}, {st})\n"
                f"  当前    {cw} × {ch}  (偏移 {cl}, {ct})\n\n"
                "坐标按物理像素记录，环境不一致会导致点击位置偏移。\n"
                "仍要继续回放吗？",
                "显示环境不一致",
                yes="继续回放",
                no="取消",
            ):
                return
        self.player.play(self.script, start_index=start, speed=self._read_speed())
        self.btn_pause.configure(state="normal", text=f"⏸  暂停  ({self.hk_pause.label()})")
        self.btn_stop.configure(state="normal")
        self.btn_play.configure(state="disabled")
        self.btn_resume.configure(state="disabled")
        self.lbl_state.configure(text="回放中", text_color=OK_GREEN)
        self.side_status.configure(text="● 回放中", text_color=OK_GREEN)

    def toggle_pause(self) -> None:
        if not self.player.playing:
            return
        self.player.toggle_pause()
        if self.player.paused:
            self.btn_pause.configure(text=f"▶  继续  ({self.hk_pause.label()})")
            self.lbl_state.configure(text="已暂停", text_color=WARN)
            self.side_status.configure(text="● 已暂停", text_color=WARN)
        else:
            self.btn_pause.configure(text=f"⏸  暂停  ({self.hk_pause.label()})")
            self.lbl_state.configure(text="回放中", text_color=OK_GREEN)
            self.side_status.configure(text="● 回放中", text_color=OK_GREEN)

    def stop_play(self) -> None:
        if self.player.playing:
            self.player.stop()

    def _on_progress(self, idx: int, total: int) -> None:
        try:
            self._q.put_nowait(("progress", idx, total))
        except queue.Full:
            pass

    def _on_finish(self, reason: str, idx: int) -> None:
        try:
            self._q.put_nowait(("finish", reason, idx))
        except queue.Full:
            pass

    def _render_progress(self, idx: int, total: int) -> None:
        self._last_executed = idx
        self.bar.set(idx / total if total else 0)
        self.lbl_prog.configure(text=f"{idx:,} / {total:,}")

    def _render_finish(self, reason: str, idx: int) -> None:
        self._last_executed = idx
        self.btn_play.configure(state="normal")
        self.btn_pause.configure(state="disabled", text=f"⏸  暂停  ({self.hk_pause.label()})")
        self.btn_stop.configure(state="disabled")
        self.btn_resume.configure(state="normal")
        if reason == "finished":
            self.lbl_state.configure(text="已完成", text_color=OK_GREEN)
            self.side_status.configure(text="● 待机", text_color=MUTED)
        elif reason == "stopped":
            self.lbl_state.configure(text="已停止", text_color=MUTED)
            self.side_status.configure(text="● 待机", text_color=MUTED)
        else:
            self.lbl_state.configure(text="异常中断", text_color=DANGER)
            self.side_status.configure(text="● 待机", text_color=MUTED)
            dialogs.error(self, reason, "回放异常")

    # ================================================================ 设置回调
    def _on_mi(self, value) -> None:
        self.move_min_interval = int(round(float(value)))
        self.lbl_mi.configure(text=f"最小间隔  {self.move_min_interval} ms")
        self.recorder.move_min_interval_us = self.move_min_interval * 1000

    def _on_md(self, value) -> None:
        self.move_min_distance = int(round(float(value)))
        self.lbl_md.configure(text=f"最小距离  {self.move_min_distance} px")
        self.recorder.move_min_distance = self.move_min_distance

    def _on_compress(self) -> None:
        self.compress_on_save = bool(self.var_compress.get())

    def restart_as_admin(self) -> None:
        """以管理员身份重新启动（用户拒绝过 UAC 时的补救入口）。"""
        from . import elevation
        if not dialogs.confirm(
            self,
            "将以管理员身份重新启动本程序，当前窗口会关闭。\n"
            "如果正在录制请先结束录制。\n\n确定要继续吗？",
            "以管理员身份重新启动",
            yes="重新启动",
            no="取消",
        ):
            return
        if self.recorder.recording:
            self.stop_record()
        try:
            if self.player.playing:
                self.player.stop()
                self.player.wait(1.5)
        except Exception:
            pass
        # 复用入口的提权流程，让它负责拉起新进程
        ok = W.relaunch_elevated([])
        if not ok:
            dialogs.warning(self, "提权失败，可能是在系统提示中选择了「否」。\n"
                                  "也可以右键程序图标，选择「以管理员身份运行」。",
                            "未能提权")
            return
        self._on_close()

    # ================================================================ 时钟
    def _tick(self) -> None:
        if self._closing:
            return
        if self.recorder.recording:
            el = time.perf_counter() - self._rec_start
            self.rec_timer.configure(text=fmt_clock(el))
            self.rec_count.configure(text=f"已记录 {self.recorder.count:,} 条操作")
        self._after(self._tick, 60, recurring=True)

    # ================================================================ 退出
    def _on_close(self) -> None:
        self._closing = True
        try:
            if self.player.playing:
                self.player.stop()
                # 等待可被打断（player 侧 WaitForMultipleObjects），
                # 长间隔中的回放也能在毫秒级退出并抬起所有按住的键
                self.player.wait(2.0)
            self.recorder.stop()
        except Exception:
            pass
        self.player.close()
        # 只取消本程序自己排的定时回调，避免它们在控件销毁后仍去访问控件。
        # 注意不能遍历 after info 全部取消：CustomTkinter 内部也用自己的
        # after 做收尾，被抢先取消后其 destroy 会抛 TclError。
        for aid in self._recurring_aids.values():
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        self._recurring_aids.clear()
        for aid in self._after_ids:
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        self._after_ids.clear()
        W.cancel_stale_tk_timers(self)
        self.destroy()


def fmt_clock(sec: float) -> str:
    ms = int(sec * 1000) % 1000
    s = int(sec) % 60
    m = int(sec) // 60
    return f"{m:02d}:{s:02d}.{ms:03d}"


# ---------------------------------------------------------------- 虚拟键名
_VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x10: "Shift", 0x11: "Ctrl", 0x12: "Alt",
    0x13: "Pause", 0x14: "CapsLock", 0x1B: "Esc", 0x20: "Space", 0x21: "PageUp", 0x22: "PageDown",
    0x23: "End", 0x24: "Home", 0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓",
    0x2C: "PrintScreen", 0x2D: "Insert", 0x2E: "Delete", 0x5B: "Win", 0x5C: "Win",
    0x60: "Num0", 0x61: "Num1", 0x62: "Num2", 0x63: "Num3", 0x64: "Num4",
    0x65: "Num5", 0x66: "Num6", 0x67: "Num7", 0x68: "Num8", 0x69: "Num9",
    0x6A: "Num*", 0x6B: "Num+", 0x6D: "Num-", 0x6E: "Num.", 0x6F: "Num/",
    0x90: "NumLock", 0x91: "ScrollLock", 0xA0: "LShift", 0xA1: "RShift",
    0xA2: "LCtrl", 0xA3: "RCtrl", 0xA4: "LAlt", 0xA5: "RAlt", 0xBA: ";", 0xBB: "=",
    0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`", 0xDB: "[", 0xDC: "\\",
    0xDD: "]", 0xDE: "'",
}


def vk_name(vk: int) -> str:
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    return _VK_NAMES.get(vk, f"VK_{vk:02X}")
