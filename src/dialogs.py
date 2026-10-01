"""深色主题对话框。

tkinter 自带的 messagebox 是系统原生控件，在深色界面里非常突兀。
这里用 CustomTkinter 做一套等效的模态对话框，外观与主界面一致。

自动化测试友好：设置 KMR_NO_DIALOG=1 环境变量（或调用 set_suppress(True)）后，
所有对话框变成记录型空操作，不会阻塞、也不会弹出窗口——
避免无人值守的测试因为一个模态框永久挂住。
"""
from __future__ import annotations

import os
import tkinter as tk

import customtkinter as ctk

from . import winapi as W
from .ui_theme import (
    ACCENT, ACCENT_HOVER, BG, CARD, CARD2, CARD_HOVER,
    DANGER, DANGER_HOVER, FONT, MUTED, OK_GREEN, TEXT, WARN,
)

# 记录被抑制的对话框，便于测试断言
SUPPRESSED: list[tuple[str, str, str]] = []
_suppress = os.environ.get("KMR_NO_DIALOG") == "1"
# 抑制模式下 confirm() 的返回值。默认 False（不确认）——在没有真人可问的情况下，
# 「不继续」永远是安全的一侧；自动化测试要放行时显式设成 True。
_suppress_confirm = False
# 当前打开着的主题化模态对话框数量。wait_window 是局部事件循环，
# after 定时器（UI 队列泵）在模态期间照常运行，全局快捷键命令会再入——
# 调用方用 modal_active() 判断后丢弃，避免在确认框底下叠出新的对话框。
_modal_depth = 0

KIND_STYLE = {
    "info": ("ⓘ", ACCENT),
    "success": ("✔", OK_GREEN),
    "warning": ("⚠", WARN),
    "error": ("✕", DANGER),
    "question": ("?", ACCENT),
}


def set_suppress(value: bool, confirm_response: bool = False) -> None:
    """开启/关闭抑制模式。

    confirm_response 决定抑制期间 confirm() 返回什么，测试可按需指定。
    """
    global _suppress, _suppress_confirm
    _suppress = bool(value)
    _suppress_confirm = bool(confirm_response)


def is_suppressed() -> bool:
    return _suppress


def modal_active() -> bool:
    """是否有主题化模态对话框正在打开（抑制模式不计入）。"""
    return _modal_depth > 0


class _Dialog(ctk.CTkToplevel):
    def __init__(self, parent, kind: str, title: str, message: str,
                 buttons: list[tuple[str, object, str]]):
        super().__init__(parent, fg_color=BG)
        self.result = None
        self.withdraw()                     # 先藏起来，布局算好再显示，避免闪烁
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)

        glyph, color = KIND_STYLE.get(kind, KIND_STYLE["info"])

        body = ctk.CTkFrame(self, fg_color=CARD, corner_radius=16)
        body.pack(fill="both", expand=True, padx=14, pady=14)

        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(18, 6))
        ctk.CTkLabel(head, text=glyph, font=(FONT, 22, "bold"), text_color=color,
                     width=30).pack(side="left", anchor="n")
        ctk.CTkLabel(head, text=title, font=(FONT, 16, "bold"), text_color=TEXT,
                     anchor="w", justify="left", wraplength=360).pack(side="left", padx=(10, 0))

        ctk.CTkLabel(body, text=message, font=(FONT, 13), text_color=MUTED,
                     anchor="w", justify="left", wraplength=420).pack(
            fill="x", padx=20, pady=(2, 10))

        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(6, 18))
        for text, value, style in buttons:
            if style == "primary":
                kw = dict(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#FFFFFF")
            elif style == "danger":
                kw = dict(fg_color=DANGER, hover_color="#C43A3F", text_color="#FFFFFF")
            else:
                kw = dict(fg_color=CARD2, hover_color="#2C3140", text_color=TEXT)
            ctk.CTkButton(row, text=text, width=100, height=36, corner_radius=9,
                          font=(FONT, 13), command=lambda v=value: self._done(v),
                          **kw).pack(side="right", padx=(8, 0))

        # Enter 触发第一个按钮（通常是确认），Esc 取消
        self.bind("<Return>", lambda e: self._done(buttons[-1][1]))
        self.bind("<Escape>", lambda e: self._done(False))
        self.protocol("WM_DELETE_WINDOW", lambda: self._done(False))

        self._center(parent)
        self.deiconify()
        self.grab_set()
        self.focus_force()

    def _center(self, parent) -> None:
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        try:
            if parent is not None and parent.winfo_exists():
                px, py = parent.winfo_rootx(), parent.winfo_rooty()
                pw, ph = parent.winfo_width(), parent.winfo_height()
            else:
                raise ValueError
        except Exception:
            left, top, vw, vh = W.vscreen()
            px, py, pw, ph = left, top, vw, vh
        x = px + (pw - w) // 2
        y = py + (ph - h) // 3
        # 保证不跑出虚拟桌面
        left, top, vw, vh = W.vscreen()
        x = max(left, min(x, left + vw - w))
        y = max(top, min(y, top + vh - h))
        self.geometry(f"+{x}+{y}")

    def _done(self, value) -> None:
        self.result = value
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def destroy(self) -> None:
        try:
            W.cancel_stale_tk_timers(self)
        except Exception:
            pass
        super().destroy()


def _run(parent, kind: str, title: str, message: str,
         buttons: list[tuple[str, object, str]], default):
    if _suppress:
        SUPPRESSED.append((kind, title, message))
        # confirm 类走专门的返回值；其余（info/warning/error）返回 True 表示"看到了"
        if kind == "question":
            return _suppress_confirm
        return default
    global _modal_depth
    _modal_depth += 1
    try:
        try:
            dlg = _Dialog(parent, kind, title, message, buttons)
            dlg.wait_window()
            return dlg.result
        except Exception:
            # 极端情况下（窗口已销毁等）退回到原生对话框，保证用户仍能看到信息
            import tkinter.messagebox as mb
            SUPPRESSED.append((kind, title, message))
            fn = {"info": mb.showinfo, "success": mb.showinfo,
                  "warning": mb.showwarning, "error": mb.showerror}.get(kind, mb.showinfo)
            if kind == "question":
                return mb.askyesno(title, message)
            fn(title, message)
            return True
    finally:
        _modal_depth -= 1


def info(parent, message: str, title: str = "提示") -> None:
    _run(parent, "info", title, message, [("知道了", True, "primary")], True)


def success(parent, message: str, title: str = "完成") -> None:
    _run(parent, "success", title, message, [("好", True, "primary")], True)


def warning(parent, message: str, title: str = "注意") -> None:
    _run(parent, "warning", title, message, [("知道了", True, "primary")], True)


def error(parent, message: str, title: str = "出错了") -> None:
    _run(parent, "error", title, message, [("关闭", True, "danger")], True)


def confirm(parent, message: str, title: str = "请确认",
            yes: str = "继续", no: str = "取消") -> bool:
    return bool(_run(parent, "question", title, message,
                     [(no, False, "ghost"), (yes, True, "primary")], False))
