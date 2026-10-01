"""测试引导的源码来源契约。"""

import json
import os
import socket
import sys
from pathlib import Path
from types import ModuleType

import pytest
from anyio import Path as AsyncPath
from anyio import create_task_group, run_process

import app.testing.bootstrap
import app.testing.network
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


def test_bootstrap_rejects_plugin_preloaded_from_another_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """确保已从其他位置加载同名插件时明确失败。"""

    bootstrap = sys.modules["_subtitleassistant_test_bootstrap"]
    foreign = ModuleType(bootstrap.PLUGIN_MODULE)
    foreign.__file__ = "/tmp/other/subtitleassistant/__init__.py"
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, bootstrap.PLUGIN_MODULE, foreign)
        with pytest.raises(RuntimeError, match="已从其他位置加载"):
            bootstrap._load_plugin_from_worktree()

    assert sys.modules[bootstrap.PLUGIN_MODULE] is subtitleassistant


async def _run_entry_probes(cwd: Path, report: Path) -> list[str]:
    """一次启动收集全量节点并执行来源及网络探针，每个入口使用独立配置目录。"""

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(HOST_ROOT)
    environment["CONFIG_DIR"] = str(report.with_suffix(".config"))
    arguments = [
        "-q",
        f"--rootdir={TEST_ROOT}",
        str(TEST_ROOT),
        "-k",
        "plugin_package_is_loaded or host_test_harness_remains_loaded",
    ]
    # 在 -k 筛选前保存全量节点，避免另起 pytest 进程只做收集。
    source = f"""
import json
from pathlib import Path

import pytest

class CollectionReport:
    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_modifyitems(self, items):
        Path({str(report)!r}).write_text(
            json.dumps(sorted(item.nodeid for item in items)), encoding="utf-8"
        )

raise SystemExit(pytest.main({arguments!r}, plugins=[CollectionReport()]))
"""
    result = await run_process(
        [sys.executable, "-c", source],
        cwd=cwd,
        env=environment,
        check=False,
    )
    output = result.stdout.decode() + result.stderr.decode()
    assert result.returncode == 0, output
    assert "2 passed" in output, output
    return json.loads(await AsyncPath(report).read_text(encoding="utf-8"))


async def test_pytest_entry_points_collect_the_same_worktree_tests(tmp_path: Path) -> None:
    """确保插件与宿主启动位置均可执行当前工作树测试。"""

    nodes: dict[str, list[str]] = {}

    async def probe(name: str, cwd: Path) -> None:
        """保存一个独立入口收集的节点。"""

        nodes[name] = await _run_entry_probes(cwd, tmp_path / f"{name}.json")

    async with create_task_group() as group:
        group.start_soon(probe, "plugin", PLUGIN_ROOT)
        group.start_soon(probe, "host", HOST_ROOT)

    assert nodes["plugin"]
    assert nodes["plugin"] == nodes["host"]
