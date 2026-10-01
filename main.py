"""PyInstaller / 直接运行入口。

直接运行：  python main.py
打包：      pyinstaller --noconfirm build.spec
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
