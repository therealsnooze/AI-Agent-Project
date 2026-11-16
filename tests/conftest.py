import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import config  # noqa: E402
from agent.executors import Executors  # noqa: E402
from agent.workspace import build_database  # noqa: E402


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(config.SEED_DIR / "files", root)
    build_database(root / "store.db")
    return root


@pytest.fixture
def ex(ws: Path) -> Executors:
    return Executors(ws)
