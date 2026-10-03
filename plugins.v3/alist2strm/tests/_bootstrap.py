"""Alist2Strm 测试进程引导。"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

PLUGIN_MODULE = "app.plugins.alist2strm"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST_ROOT = Path("/home/zhang/Documents/MoviePilot")


def _host_root() -> Path:
    """返回测试使用的 MoviePilot 宿主根目录。"""

    configured = os.environ.get("MOVIEPILOT_ROOT")
    return Path(configured).expanduser().resolve() if configured else DEFAULT_HOST_ROOT


def _load_plugin_from_worktree() -> ModuleType:
    """把当前工作树加载为宿主使用的插件包名。"""

    package_init = (PLUGIN_ROOT / "__init__.py").resolve()
    loaded = sys.modules.get(PLUGIN_MODULE)
    if loaded is not None:
        loaded_file = getattr(loaded, "__file__", None)
        if loaded_file and Path(loaded_file).resolve() == package_init:
            return loaded
        raise RuntimeError(f"插件包 {PLUGIN_MODULE} 已从其他位置加载：{loaded_file}")
    spec = importlib.util.spec_from_file_location(
        PLUGIN_MODULE,
        package_init,
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法创建插件包加载器：{package_init}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PLUGIN_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(PLUGIN_MODULE, None)
        raise
    return module


def prepare_plugin_backend() -> None:
    """准备宿主隔离后端并加载当前工作树插件。"""

    host_root = _host_root()
    if not host_root.is_dir():
        raise RuntimeError(f"MoviePilot 宿主目录不存在：{host_root}")
    host_value = str(host_root)
    sys.path[:] = [item for item in sys.path if item != host_value]
    sys.path.insert(0, host_value)
    from app.testing.bootstrap import prepare_backend

    prepare_backend()
    _load_plugin_from_worktree()
