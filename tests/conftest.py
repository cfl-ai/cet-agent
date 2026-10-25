"""
pytest 公共配置。

关键点：每个测试都在独立的临时目录中运行。
由于 DB_PATH 是相对路径 "data/cet_agent.db"，chdir 之后各模块的
数据库连接会自动指向临时目录，从而与真实数据完全隔离。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    """隔离工作目录，使测试库与真实库互不干扰"""
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)
    # main.py 挂载了 StaticFiles(directory="static")，导入时该目录必须存在
    os.makedirs("static", exist_ok=True)
    yield tmp_path


@pytest.fixture()
def event_loop_policy():
    import asyncio
    return asyncio.DefaultEventLoopPolicy()
