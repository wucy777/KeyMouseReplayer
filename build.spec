# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

产出单文件 exe，不带控制台窗口。
CustomTkinter 的主题资源是数据文件，必须显式收集，否则打包后界面会变成空白。
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

APP_NAME = "KeyMouseReplayer"

datas = []
datas += collect_data_files("customtkinter")
# 自带图标与资源
datas += [("assets/app.ico", "assets"), ("assets/app.png", "assets")]

hiddenimports = collect_submodules("customtkinter")

a = Analysis(
    ["main.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 明确排除用不到的重型库，显著缩小体积
    excludes=[
        "numpy", "PIL", "matplotlib", "scipy", "pandas",
        "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
        "pytest", "setuptools", "pip", "unittest",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 带管理员清单：目标程序若以管理员运行，非提升会收不到/发不出输入
    # uac_admin=False：不动清单，改由 src/elevation.py 在运行时静默重新拉起自己。
    # 好处是启动前不建任何窗口，用户双击后直接看到系统 UAC 确认框，
    # 确认完出现的第一个界面就已经是管理员权限，中途没有任何多余窗口；
    # 用户在 UAC 里选「否」时程序仍能以普通权限启动（而不是直接失败）。
    # 若想连这一步 Windows 确认都由系统在进程启动前完成，把这里改成 True 即可
    # （代价：拒绝 UAC 时程序完全无法启动，且不利于自动化验证）。
    uac_admin=False,
    version="version_info.txt",
    icon="assets/app.ico",
)
