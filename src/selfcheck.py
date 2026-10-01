"""内置自检：用于验证打包后的 exe 是否完整可用。

打包成无控制台的 exe 后，异常不会显示在屏幕上，所以提供一个可写的自检模式：
    KeyMouseReplayer.exe --check            结果打印到标准输出（源码运行时用）
    KeyMouseReplayer.exe --check --out f.json   结果写入文件（exe 用这个）

检查项覆盖打包最容易出问题的地方：CustomTkinter 主题资源、Tk 可用性、
钩子安装、脚本序列化、回放时间轴。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

from .version import APP_NAME, __version__


def _check(results: list, name: str, fn) -> None:
    t = time.perf_counter()
    try:
        detail = fn()
        results.append({"name": name, "ok": True,
                        "detail": str(detail) if detail else "",
                        "ms": round((time.perf_counter() - t) * 1000, 1)})
    except Exception as exc:
        results.append({"name": name, "ok": False,
                        "detail": f"{exc}\n{traceback.format_exc()}",
                        "ms": round((time.perf_counter() - t) * 1000, 1)})


def run() -> tuple[bool, list]:
    from . import winapi as W
    W.set_dpi_awareness()

    # 自检必须无人值守：一开始就把主题化对话框切成抑制模式。
    # 注意不能只 patch tkinter.messagebox —— 界面已全部改用 dialogs，
    # 漏掉这步会让「启动自检」弹出真正的模态框，把自检永久挂住。
    from . import dialogs
    dialogs.set_suppress(True)

    results: list = []

    # 1. 关键模块是否都被打进去
    def imports():
        import array  # noqa: F401
        import ctypes  # noqa: F401
        import queue  # noqa: F401
        import tkinter  # noqa: F401
        import zlib  # noqa: F401
        import customtkinter as ctk
        return f"customtkinter {ctk.__version__}, tkinter {tkinter.TkVersion}"

    _check(results, "关键模块导入", imports)

    # 2. CustomTkinter 主题资源（打包最常丢的东西）
    def ctk_assets():
        import customtkinter as ctk
        base = os.path.dirname(ctk.__file__)
        # 这些是界面渲染依赖的真实资源：主题 json、图标、字体
        need = [
            "assets/themes/dark-blue.json",
            "assets/icons/CustomTkinter_icon_Windows.ico",
            "assets/fonts/CustomTkinter_shapes_font.otf",
        ]
        missing = [rel for rel in need
                   if not os.path.exists(os.path.join(base, rel.replace("/", os.sep)))]
        if missing:
            raise RuntimeError("缺少资源: " + ", ".join(missing))
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        return f"{len(need)} 项资源齐全 @ {base}"

    _check(results, "CustomTkinter 资源", ctk_assets)

    # 3. 能真正建出一个窗口并渲染（验证 Tcl/Tk 数据文件齐全）
    def tk_window():
        import customtkinter as ctk
        ctk.deactivate_automatic_dpi_awareness()
        root = ctk.CTk()
        root.geometry("320x200")
        lbl = ctk.CTkLabel(root, text="check")
        lbl.pack()
        root.update()
        root.update_idletasks()
        W.cancel_stale_tk_timers(root)
        root.destroy()
        return "窗口创建/渲染/销毁正常"

    _check(results, "Tk 窗口渲染", tk_window)

    # 4. 全局键盘钩子 + Raw Input
    def hooks():
        from .recorder import Recorder
        rec = Recorder()
        rec.start()
        ok_kb = rec._kb_hook is not None
        rec.start_recording()
        ok_ms = rec._ms_hook is not None
        ok_raw = rec.raw_ok
        rec.stop_recording()
        rec.stop()
        if not ok_kb:
            raise RuntimeError("键盘钩子安装失败")
        if not ok_ms:
            raise RuntimeError("鼠标钩子安装失败")
        if not ok_raw:
            raise RuntimeError("Raw Input 注册失败")
        return "键盘/鼠标钩子与 Raw Input 均可用"

    _check(results, "全局钩子与 Raw Input", hooks)

    # 5. 脚本序列化往返
    def serialize():
        import array as _arr
        from . import script_io
        from .script_io import STRIDE, Script
        ev = _arr.array("i")
        for i in range(5000):
            ev.extend((W.K_MOUSE_MOVE, 100 + i % 60, 200 + i % 40, 0, 8000, 0))
        s = Script(events=ev, count=5000, screen=W.refresh_virtual_screen())
        blob = script_io.serialize(s, compress=True)
        back = script_io.deserialize(blob)
        if back.events != s.events or back.count != 5000:
            raise RuntimeError("往返数据不一致")
        return f"5000 条往返一致，压缩后 {len(blob) / 1024:.1f} KB"

    _check(results, "脚本序列化", serialize)

    # 6. 坐标换算
    def coords():
        left, top, width, height = W.refresh_virtual_screen()
        if W.to_absolute(left, top) != (0, 0):
            raise RuntimeError("左上角换算错误")
        if W.to_absolute(left + width - 1, top + height - 1) != (65535, 65535):
            raise RuntimeError("右下角换算错误")
        return f"虚拟桌面 ({left},{top}) {width}x{height}"

    _check(results, "坐标换算", coords)

    # 7. 回放时间轴（干跑）
    def timeline():
        import array as _arr
        from .player import Player
        from .script_io import Script
        ev = _arr.array("i")
        for _ in range(20):
            ev.extend((W.K_MOUSE_MOVE, 100, 200, 0, 50_000, 0))
        s = Script(events=ev, count=20, screen=W.refresh_virtual_screen())
        p = Player()
        p.dry_run = True
        done = []
        p.on_finish = lambda r, i: done.append((r, i))
        t = time.perf_counter()
        p.play(s, 0, 2.0)
        p.wait(10)
        el = time.perf_counter() - t
        if not done or done[0][1] != 20:
            raise RuntimeError(f"未跑完: {done}")
        if abs(el - 0.5) > 0.08:
            raise RuntimeError(f"时间轴偏差过大: {el:.3f}s (期望 0.5s)")
        return f"20 条 2x 干跑 {el:.3f}s (期望 0.500s)"

    _check(results, "回放时间轴", timeline)

    # 8. 完整 UI 构建
    def ui():
        # 对话框抑制已在 run() 开头统一打开；这里再显式确认一次
        from . import dialogs
        dialogs.set_suppress(True)
        from .ui import App
        app = App()
        for _ in range(20):
            app.update()
        # 触发启动自检与一次完整刷新，确保这些路径不会抛异常
        app._startup_check()
        app._show("script")
        app._show("settings")
        for _ in range(10):
            app.update()
        app.recorder.stop()
        app._on_close()
        return "主界面构建、页面切换与启动自检正常"

    _check(results, "主界面构建", ui)

    # 9. 主题化对话框（替代原生 messagebox）
    def themed_dialogs():
        from . import dialogs
        dialogs.set_suppress(True)
        n_before = len(dialogs.SUPPRESSED)
        from .ui import App
        app = App()
        for _ in range(6):
            app.update()
        dialogs.info(app, "自检")
        dialogs.warning(app, "自检")
        dialogs.error(app, "自检")
        r = dialogs.confirm(app, "自检")
        n = len(dialogs.SUPPRESSED) - n_before
        app.recorder.stop()
        app._on_close()
        if n < 4:
            raise RuntimeError(f"对话框未被记录，仅 {n} 条")
        return f"4 类对话框可用（抑制模式记录 {n} 条）"

    _check(results, "主题化对话框", themed_dialogs)

    # 10. 提权相关 API 可用
    def elevation_api():
        from . import elevation
        ready, how = elevation.ensure_elevated([elevation.NO_ELEVATE_FLAG])
        return f"当前管理员={W.is_elevated()} 跳过路径={how}"

    _check(results, "提权检查接口", elevation_api)

    # 11. 随程序分发的资源（打包后最易缺失）
    def bundled_assets():
        from .ui import resource_path
        missing = []
        for rel in ("assets/app.ico", "assets/app.png"):
            if not os.path.exists(resource_path(rel)):
                missing.append(rel)
        if missing:
            raise RuntimeError("缺少资源: " + ", ".join(missing))
        return f"图标资源齐全 @ {resource_path('assets/app.ico')}"

    _check(results, "程序自带资源", bundled_assets)

    return all(r["ok"] for r in results), results


def main(argv: list[str]) -> int:
    out = None
    if "--out" in argv:
        i = argv.index("--out")
        if i + 1 < len(argv):
            out = argv[i + 1]
    try:
        ok, results = run()
    except Exception:
        ok = False
        results = [{"name": "自检启动", "ok": False, "detail": traceback.format_exc(), "ms": 0}]

    lines = []
    lines.append(f"{APP_NAME} v{__version__} 自检")
    lines.append(f"解释器 {sys.version.split()[0]}  frozen={getattr(sys, 'frozen', False)}")
    lines.append("")
    for r in results:
        lines.append(f"[{'OK' if r['ok'] else 'FAIL'}] {r['name']} ({r['ms']}ms) {r['detail']}")
    lines.append("")
    lines.append("全部通过" if ok else "存在失败项")
    text = "\n".join(lines)

    if out:
        try:
            with open(out, "w", encoding="utf-8") as fh:
                json.dump({"ok": ok, "results": results}, fh, ensure_ascii=False, indent=2)
        except OSError:
            # 写不出结果文件（路径含空格被截断、无权限等）不应变成无控制台
            # 程序里的报错弹窗；检查结论已经得出，照常走退出码
            results.append({"name": "--out 写入", "ok": False,
                            "detail": f"无法写入 {out!r}", "ms": 0})
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    if out:
        try:
            with open(out + ".txt", "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            pass
    return 0 if ok else 1
