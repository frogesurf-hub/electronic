"""独立文档准备入口；允许复用本机另一套 PDF 工具 Python 环境。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_core.retrieval.preparation import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
