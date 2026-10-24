"""未安装时的入口。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from lftcli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
