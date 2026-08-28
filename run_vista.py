#!/usr/bin/env python3
"""免安装入口。

如果你不想 `pip install -e .`，可以直接：

    python run_vista.py demo
    python run_vista.py run "任务描述"

它做的事只有一件：把 src/ 加进 sys.path，然后调用 vista.__main__:main。

注意文件名不能叫 vista.py —— 那会在仓库根目录遮蔽 src/vista 这个包，
导致 `import vista` 拿到的是这个脚本而不是包本身。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from vista.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
