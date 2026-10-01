"""测试引导的源码来源契约。"""

import os
import socket
import sys
from pathlib import Path

import app.testing.bootstrap
import app.testing.network
import pytest
from anyio import run_process
from app.plugins import subtitleassistant
from app.plugins.subtitleassistant.schemas import target as target_schema

HOST_ROOT = Path("/home/zhang/Documents/MoviePilot")
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = PLUGIN_ROOT / "tests"

pytestmark = pytest.mark.anyio


async def test_plugin_package_is_loaded_from_current_checkout() -> None:
    """确保测试不会静默使用 MoviePilot 主工作区中的插件代码。"""

    expected = Path(__file__).resolve().parents[1] / "__init__.py"
    assert Path(subtitleassistant.__file__).resolve() == expected.resolve()
    assert Path(target_schema.__file__).resolve().is_relative_to(PLUGIN_ROOT)


async def test_host_test_harness_remains_loaded_from_moviepilot() -> None:
    """确保仅替换字幕助手包，宿主测试能力仍来自 MoviePilot。"""

    assert Path(app.testing.bootstrap.__file__).resolve().is_relative_to(HOST_ROOT)
    assert Path(app.testing.network.__file__).resolve().is_relative_to(HOST_ROOT)
    assert socket.getaddrinfo.__name__ == "_guarded_getaddrinfo"


async def _run_bootstrap_probe(source: str) -> tuple[int, str, str]:
    """在独立解释器中探测会影响全局导入状态的失败路径。"""

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(HOST_ROOT)
    environment["MOVIEPILOT_ROOT"] = str(HOST_ROOT)
    result = await run_process(
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=False,
    )
    return result.returncode, result.stdout.decode(), result.stderr.decode()


async def test_bootstrap_rejects_plugin_preloaded_from_another_location() -> None:
    """确保已从其他位置加载同名插件时明确失败。"""

    bootstrap = Path(__file__).with_name("_bootstrap.py").resolve()
    source = f"""
import importlib.util
import sys
from types import ModuleType

spec = importlib.util.spec_from_file_location("probe_bootstrap", {str(bootstrap)!r})
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
foreign = ModuleType(module.PLUGIN_MODULE)
foreign.__file__ = "/tmp/other/subtitleassistant/__init__.py"
sys.modules[module.PLUGIN_MODULE] = foreign
try:
    module.prepare_plugin_backend()
except RuntimeError as error:
    print(error)
else:
    raise SystemExit("bootstrap unexpectedly accepted foreign plugin")
"""

    returncode, stdout, stderr = await _run_bootstrap_probe(source)

    assert returncode == 0, stderr
    assert "已从其他位置加载" in stdout


async def _collect_tests(cwd: Path, *arguments: str) -> list[str]:
    """从指定入口收集测试并返回稳定的节点列表。"""

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(HOST_ROOT)
    result = await run_process(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            f"--rootdir={TEST_ROOT}",
            *arguments,
        ],
        cwd=cwd,
        env=environment,
        check=False,
    )
    stderr = result.stderr.decode()
    assert result.returncode == 0, stderr
    return sorted(line for line in result.stdout.decode().splitlines() if "::" in line)


async def _run_entry_probes(cwd: Path, test_path: str) -> str:
    """从指定入口执行源码来源与宿主网络守卫探针。"""

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(HOST_ROOT)
    result = await run_process(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"--rootdir={TEST_ROOT}",
            test_path,
            "-k",
            "plugin_package_is_loaded or host_test_harness_remains_loaded",
        ],
        cwd=cwd,
        env=environment,
        check=False,
    )
    stderr = result.stderr.decode()
    assert result.returncode == 0, stderr
    return result.stdout.decode()


async def test_pytest_entry_points_collect_the_same_worktree_tests() -> None:
    """确保插件与宿主启动位置均可执行当前工作树测试。"""

    plugin_nodes = await _collect_tests(PLUGIN_ROOT, str(TEST_ROOT))
    host_nodes = await _collect_tests(HOST_ROOT, str(TEST_ROOT))
    plugin_probe = await _run_entry_probes(PLUGIN_ROOT, str(TEST_ROOT / "test_bootstrap.py"))
    host_probe = await _run_entry_probes(HOST_ROOT, str(TEST_ROOT / "test_bootstrap.py"))

    assert plugin_nodes
    assert plugin_nodes == host_nodes
    assert "2 passed" in plugin_probe
    assert "2 passed" in host_probe
