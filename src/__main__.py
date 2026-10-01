"""程序入口。

启动顺序（每一步都不能颠倒）：

1. 设置 DPI 感知 —— 必须在创建任何窗口、安装任何钩子之前，
   否则坐标会被系统按缩放比例虚拟化，注入全部偏移。
2. 如果当前不是管理员：**在创建任何窗口之前**静默重新拉起自己并立即退出。
   于是用户双击后看到的就是系统的 UAC 确认框，确认完出现的第一个界面
   已经是管理员权限——中间没有任何多余窗口，也不会有旧进程窗口闪一下。
3. 自检分支（打包后无控制台，异常看不到，需要这个出口）。
4. 创建并运行界面。
"""
from __future__ import annotations

import sys


def _run_gui(elevated: bool) -> int:
    import customtkinter as ctk
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    ctk.deactivate_automatic_dpi_awareness()

    from .ui import App
    app = App(elevated=elevated)
    app.mainloop()
    return 0


def main() -> int:
    from . import elevation, winapi as W

    W.set_dpi_awareness()

    argv = sys.argv[1:]

    # 打包后是无控制台程序，异常不会显示；提供 --check 自检模式便于验证产物完整性。
    # 自检不注入任何输入，无需管理员权限，因此放在提权之前。
    if "--check" in argv:
        from .selfcheck import main as check_main
        return check_main(sys.argv)

    # 提权：必须在建任何窗口之前完成。
    # 成功拉起新进程时旧进程立刻退出，保证「首次出现的界面」就是管理员权限。
    if not W.is_elevated() and not elevation.should_skip(argv):
        ready, how = elevation.ensure_elevated(argv)
        if how == "relaunched":
            return 0                     # 新进程已接管，本进程静默退出

    # 走到这里说明：本来就是管理员；或用户拒绝了 UAC / 显式跳过。
    # 后两种情况仍可用，只是无法作用于以管理员运行的目标程序，界面会给出提示。
    return _run_gui(W.is_elevated())


if __name__ == "__main__":
    sys.exit(main())
