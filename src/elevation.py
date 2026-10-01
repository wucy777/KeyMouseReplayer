"""启动时的管理员提权。

背景：目标程序若以管理员身份运行，Windows 的 UIPI（用户界面特权隔离）会阻止
低完整性级别的进程与它交换输入——本程序既收不到它的键鼠事件（录不到），
也无法把 SendInput 注入给它（回放无效）。因此本程序需要在显示任何界面之前
就以管理员身份运行。

做法：在创建任何窗口之前检查权限，不足则用 ShellExecuteW("runas") 重新拉起自己，
然后旧进程立即静默退出。这样用户双击 exe 后**首次出现的界面就已经是管理员权限**，
不存在「先开一个普通窗口、再重启一次」的观感。

唯一无法绕过的是系统 UAC 确认框——那是 Windows 的安全边界。
若想连这一次点击都省掉，可在 exe 的「属性 → 兼容性」里勾选「以管理员身份运行」，
由系统在启动时直接提权（本模块检测到已是管理员后会直接跳过，不会重复拉起）。
"""
from __future__ import annotations

import sys

from . import winapi as W

# 防止无限重启：拉起过一次就不再尝试。
# 用命令行开关而不是环境变量来传递——ShellExecuteW 拉起的新进程虽然会继承
# 环境，但显式传参更可靠，不受权限提升时环境被重建的影响。
_ATTEMPTED_FLAG = "--elevation-attempted"

# 命令行开关：显式跳过提权（调试、自检用）
NO_ELEVATE_FLAG = "--no-elevate"


def should_skip(argv: list[str]) -> bool:
    """是否应当跳过提权。"""
    if NO_ELEVATE_FLAG in argv:
        return True
    if _ATTEMPTED_FLAG in argv:
        return True
    return False


def ensure_elevated(argv: list[str] | None = None) -> tuple[bool, str]:
    """确保以管理员身份运行。

    返回 (已就绪, 说明)：
      (True,  "already")    本来就是管理员，正常继续
      (True,  "relaunched") 已成功拉起管理员进程，调用方应立即退出自己
      (False, "declined")   用户拒绝了 UAC，调用方决定如何提示
      (False, "failed")     拉起失败
      (False, "skipped")    显式跳过（--no-elevate / 已尝试过）
    """
    args = list(argv if argv is not None else sys.argv[1:])

    if W.is_elevated():
        return True, "already"

    if should_skip(args):
        return False, "skipped"

    child_args = [a for a in args if a != _ATTEMPTED_FLAG] + [_ATTEMPTED_FLAG]
    if W.relaunch_elevated(child_args):
        return True, "relaunched"
    return False, "declined"
