"""字幕助手测试引导。

把 MoviePilot 宿主根目录放到导入路径首位、准备隔离的测试后端，再把当前工作树加载为
宿主实际使用的插件包名 ``app.plugins.subtitleassistant``，使测试导入的插件源码与运行时一致。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

# 插件在宿主中的完整包名与当前工作树内的插件目录
PLUGIN_MODULE = "app.plugins.subtitleassistant"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST_ROOT = Path("/home/zhang/Documents/MoviePilot")


def _host_root() -> Path:
    """返回测试使用的 MoviePilot 宿主根目录。"""
    configured = os.environ.get("MOVIEPILOT_ROOT")
    return Path(configured).expanduser().resolve() if configured else DEFAULT_HOST_ROOT


def _ensure_host_importable(host_root: Path) -> None:
    """把 MoviePilot 宿主根目录放到导入路径首位。"""
    if not host_root.is_dir():
        raise RuntimeError(f"MoviePilot 宿主目录不存在：{host_root}")
    value = str(host_root)
    sys.path[:] = [item for item in sys.path if item != value]
    sys.path.insert(0, value)


def _load_plugin_from_worktree() -> ModuleType:
    """把当前工作树加载为宿主实际使用的插件包名。"""
    package_init = (PLUGIN_ROOT / "__init__.py").resolve()
    loaded = sys.modules.get(PLUGIN_MODULE)
    if loaded is not None:
        loaded_file = getattr(loaded, "__file__", None)
        if loaded_file and Path(loaded_file).resolve() == package_init:
            return loaded
        raise RuntimeError(f"插件包 {PLUGIN_MODULE} 已从其他位置加载：{loaded_file}；请在测试进程启动前清理该模块")

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
        # 加载失败时清理部分导入，避免污染后续会话
        sys.modules.pop(PLUGIN_MODULE, None)
        raise
    return module


def _configure_system_config_service() -> None:
    """为插件测试进程配置系统配置服务。

    V3 宿主把系统配置服务从隐式全局改为组合根显式装配，识别链（MetaInfo 读取
    自定义制作组等规则）在构造时惰性取用该服务。生产环境由宿主启动时装配，
    测试进程在此补齐同一入口，快照已由宿主 ``prepare_backend`` 加载。
    """

    from app.application.configuration import SystemConfigService, configure_system_config
    from app.db.oper.systemconfig import SystemConfigOper

    configure_system_config(SystemConfigService(repository=SystemConfigOper()))


def prepare_plugin_backend() -> None:
    """准备宿主测试后端、装配插件所需宿主服务并加载当前工作树中的字幕助手。"""
    _ensure_host_importable(_host_root())
    from app.testing.bootstrap import prepare_backend

    prepare_backend()
    _configure_system_config_service()
    _load_plugin_from_worktree()
