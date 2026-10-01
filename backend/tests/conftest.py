"""每个用例前把内存仓库恢复到种子状态，避免跨用例污染。"""
from __future__ import annotations

import pytest

from app.store import store


@pytest.fixture(autouse=True)
def reset_store() -> None:
    store.reset()
