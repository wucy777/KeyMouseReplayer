"""启动流程验证：确认提权发生在任何窗口创建之前。

这是「双击 exe 后第一个界面就是管理员」的关键约束：
如果先建了窗口再提权，用户会看到旧窗口闪一下或同时出现两个窗口。
本测试通过替换入口用到的函数引用来验证顺序，不真的弹 UAC。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import winapi as W

FAIL = []


def check(name, ok, detail=""):
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        FAIL.append(name)


import src.__main__ as entry  # noqa: E402
from src import elevation  # noqa: E402

real_is_elevated = W.is_elevated
real_relaunch = W.relaunch_elevated
real_gui = entry._run_gui


def patch(elevated_value, relaunch_ok):
    """把入口依赖的三处外部行为换成假的，返回事件记录表。

    entry.main 内部是 `from . import elevation, winapi as W`，
    拿的是模块对象，所以替换模块属性就能影响它。
    """
    events = []

    def fake_relaunch(args=None):
        events.append("relaunch")
        return relaunch_ok

    def fake_gui(elevated):
        events.append("gui(elevated=%s)" % elevated)
        return 0

    W.relaunch_elevated = fake_relaunch
    W.is_elevated = lambda: elevated_value
    entry._run_gui = fake_gui
    return events


def restore():
    W.is_elevated = real_is_elevated
    W.relaunch_elevated = real_relaunch
    entry._run_gui = real_gui


print("\n[1] 非管理员：必须先重新拉起，且不得创建界面")
events = patch(False, True)
try:
    sys.argv = ["KeyMouseReplayer.exe"]
    rc = entry.main()
    check("先重新拉起再退出", events == ["relaunch"], str(events))
    check("拉起成功后返回 0", rc == 0, str(rc))
    check("没有创建界面", not any(e.startswith("gui") for e in events), str(events))
finally:
    restore()

print("\n[2] 已是管理员：直接进界面，不重复提权")
events = patch(True, True)
try:
    sys.argv = ["KeyMouseReplayer.exe"]
    rc = entry.main()
    check("直接进界面", events == ["gui(elevated=True)"], str(events))
    check("返回 0", rc == 0, str(rc))
finally:
    restore()

print("\n[3] 用户拒绝 UAC：仍以普通权限启动")
events = patch(False, False)
try:
    sys.argv = ["KeyMouseReplayer.exe"]
    rc = entry.main()
    # 顺序应为：尝试拉起 -> 失败 -> 以普通权限进界面
    check("尝试过拉起", events and events[0] == "relaunch", str(events))
    check("进入普通权限界面", events[-1] == "gui(elevated=False)", str(events))
    check("只进一次界面", events.count("gui(elevated=False)") == 1, str(events))
    check("返回 0", rc == 0, str(rc))
finally:
    restore()

print("\n[4] 防无限重启：带 attempted 标记时不再拉起")
events = patch(False, True)
try:
    sys.argv = ["KeyMouseReplayer.exe", elevation._ATTEMPTED_FLAG]
    rc = entry.main()
    check("不再尝试拉起", "relaunch" not in events, str(events))
    check("直接进普通权限界面", events == ["gui(elevated=False)"], str(events))
finally:
    restore()

print("\n[5] --no-elevate 显式跳过")
events = patch(False, True)
try:
    sys.argv = ["KeyMouseReplayer.exe", elevation.NO_ELEVATE_FLAG]
    rc = entry.main()
    check("不再尝试拉起", "relaunch" not in events, str(events))
    check("直接进普通权限界面", events == ["gui(elevated=False)"], str(events))
finally:
    restore()

print("\n[6] --check 不触发提权（自检无需管理员）")
events = patch(False, True)
ran = []
import src.selfcheck as sc  # noqa: E402

real_check_main = sc.main
try:
    sc.main = lambda a: (ran.append(1), 0)[1]
    sys.argv = ["KeyMouseReplayer.exe", "--check"]
    rc = entry.main()
    check("走自检分支", ran == [1], "ran=%s" % ran)
    check("不触发提权", "relaunch" not in events, str(events))
    check("不创建界面", not any(e.startswith("gui") for e in events), str(events))
finally:
    sc.main = real_check_main
    restore()

print("\n" + "=" * 50)
if FAIL:
    print("失败 %d 项：%s" % (len(FAIL), ", ".join(FAIL)))
    sys.exit(1)
print("启动流程验证全部通过")
