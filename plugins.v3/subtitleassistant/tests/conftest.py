"""SubtitleAssistant pytest 测试引导。"""

import importlib.util
import sys
from pathlib import Path

import pytest

_BOOTSTRAP_PATH = Path(__file__).with_name("_bootstrap.py")
_BOOTSTRAP_SPEC = importlib.util.spec_from_file_location("_subtitleassistant_test_bootstrap", _BOOTSTRAP_PATH)
if _BOOTSTRAP_SPEC is None or _BOOTSTRAP_SPEC.loader is None:
    raise ImportError(f"无法创建测试引导加载器：{_BOOTSTRAP_PATH}")
_BOOTSTRAP = importlib.util.module_from_spec(_BOOTSTRAP_SPEC)
sys.modules[_BOOTSTRAP_SPEC.name] = _BOOTSTRAP
_BOOTSTRAP_SPEC.loader.exec_module(_BOOTSTRAP)
_BOOTSTRAP.prepare_plugin_backend()

from app.testing.network import block_real_network  # noqa: F401


@pytest.fixture
def anyio_backend() -> str:
    """固定使用 asyncio 运行 AnyIO 异步测试。"""

    return "asyncio"
