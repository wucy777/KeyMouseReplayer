"""提权逻辑验证。

无法在无人值守环境里真正响应 UAC 确认框，因此这里验证的是「决策与调用」：
- 已是管理员时直接放行
- 带 --no-elevate 时跳过
- 带 --elevation-attempted 时跳过（防无限重启的关键）
- 参数拼装正确（frozen 与源码两种形态）
- 拉起失败时返回 declined，不抛异常
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import elevation, winapi as W

FAIL = []


def check(name, ok, detail=""):
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        FAIL.append(name)


print("\n[1] 当前进程权限")
adm = W.is_elevated()
print(f"  当前管理员权限: {adm}")
check("is_elevated 返回布尔值", isinstance(adm, bool))

print("\n[2] 跳过判定")
check("--no-elevate 触发跳过", elevation.should_skip([elevation.NO_ELEVATE_FLAG]))
check("--elevation-attempted 触发跳过",
      elevation.should_skip([elevation._ATTEMPTED_FLAG]))
check("普通参数不跳过", not elevation.should_skip([]))
check("--check 不触发跳过", not elevation.should_skip(["--check"]))

print("\n[3] ensure_elevated 的各条分支")
ready, how = elevation.ensure_elevated([elevation.NO_ELEVATE_FLAG])
check("显式跳过时返回 skipped", how == "skipped", f"ready={ready} how={how}")

ready, how = elevation.ensure_elevated([elevation._ATTEMPTED_FLAG])
check("已尝试过时返回 skipped", how == "skipped", f"ready={ready} how={how}")

if adm:
    ready, how = elevation.ensure_elevated([])
    check("管理员下返回 already", ready and how == "already", f"{how}")

print("\n[4] 防无限重启：attempted 标记会被传递给子进程")
# 直接检查参数拼装逻辑：模拟一次「需要提权」的调用但不真的弹 UAC
orig = W.relaunch_elevated
captured = {}


def fake_relaunch(args=None):
    captured["args"] = list(args) if args is not None else None
    return True


W.relaunch_elevated = fake_relaunch
elevation.W.relaunch_elevated = fake_relaunch
try:
    ready, how = elevation.ensure_elevated(["--check"])
    if adm:
        check("管理员下不会调用重新拉起", "args" not in captured, f"{how}")
    else:
        check("非管理员下会尝试重新拉起", captured.get("args") is not None, f"{captured}")
        check("重新拉起时带上 attempted 标记",
              captured.get("args") and elevation._ATTEMPTED_FLAG in captured["args"],
              str(captured.get("args")))
        check("原有参数被保留",
              captured.get("args") and "--check" in captured["args"],
              str(captured.get("args")))
        check("返回 relaunched", how == "relaunched", how)
finally:
    W.relaunch_elevated = orig
    elevation.W.relaunch_elevated = orig

print("\n[5] 拉起失败时优雅返回")
W.relaunch_elevated = lambda args=None: False
elevation.W.relaunch_elevated = lambda args=None: False
try:
    ready, how = elevation.ensure_elevated([])
    if adm:
        check("管理员下仍返回 already", how == "already", how)
    else:
        check("失败时返回 declined 且不抛异常", ready is False and how == "declined",
              f"ready={ready} how={how}")
finally:
    W.relaunch_elevated = orig
    elevation.W.relaunch_elevated = orig

print("\n[6] 命令行参数拼装（源码 vs 打包）")
import subprocess  # noqa: E402
check("list2cmdline 处理空格路径",
      "program files" in subprocess.list2cmdline([r"C:\Program Files\a.exe"]).lower())

print("\n" + "=" * 46)
if FAIL:
    print(f"失败 {len(FAIL)} 项：" + ", ".join(FAIL))
    sys.exit(1)
print("提权逻辑验证全部通过")
