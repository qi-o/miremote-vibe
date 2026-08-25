"""打包启动器：让 PyInstaller 从顶层脚本进入，但 miremote 保持为完整包。

PyInstaller 直接打包包内的 __main__.py 会丢失包上下文导致相对导入失败。
此脚本作为入口，把项目根目录加进 sys.path 后导入完整包。
"""

import os
import sys

# 打包后（frozen）PyInstaller 已把包内模块放进内置路径；源码运行时把根目录加进 path
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from miremote.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
