"""插件管理 API 的认证与保存契约测试。"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from app.api.dependencies.auth import get_current_active_superuser_async
from app.plugins.alist2strm import Alist2Strm  # ty: ignore[unresolved-import]
from app.plugins.alist2strm.config import build_configuration  # ty: ignore[unresolved-import]


def _configuration() -> object:
    """构造仅含合成 Token 标记的测试配置。"""

    return build_configuration(
        {
            "instances": [
                {
                    "id": "instance-1",
                    "name": "实例",
                    "scan_url": "https://openlist.example.invalid",
                    "api_token": "configured",
                    "mappings": [{"source_dir": "/library", "output_dir": "/strm"}],
                }
            ]
        }
    )


def _dependency(endpoint: Any) -> Any:
    """读取端点下划线参数的 FastAPI 依赖。"""

    parameter = inspect.signature(endpoint).parameters["_"]
    default = parameter.default
    assert hasattr(default, "dependency")
    return default.dependency


def test_all_management_routes_use_bearer_and_explicit_superuser_dependency() -> None:
    """配置读写 API 都必须声明 Bearer 与超级管理员依赖。"""

    plugin = object.__new__(Alist2Strm)
    routes = plugin.get_api()

    assert {(item["path"], tuple(item["methods"])) for item in routes} == {
        ("/config", ("GET",)),
        ("/config", ("PUT",)),
        ("/scan", ("POST",)),
        ("/scan/mapping", ("POST",)),
        ("/scan/status", ("GET",)),
        ("/history", ("GET",)),
        ("/scan/stop", ("POST",)),
    }
    assert all(item["auth"] == "bear" for item in routes)
    assert all(_dependency(item["endpoint"]) is get_current_active_superuser_async for item in routes)


def test_read_configuration_never_returns_moviepilot_or_openlist_token_value() -> None:
    """配置读取投影只返回是否已配置，不下发 Token。"""

    plugin = object.__new__(Alist2Strm)
    plugin._configuration = _configuration()
    plugin._configuration_valid = True
    response = plugin.read_config(_=object())

    assert response["success"] is True
    instance = response["data"]["instances"][0]
    assert instance["api_token_configured"] is True
    assert "api_token" not in instance
    assert "moviepilot" not in str(response).lower()


def test_invalid_save_returns_validation_error_without_calling_persistence() -> None:
    """不合法目录在保存前失败且不进入宿主配置写入。"""

    plugin = object.__new__(Alist2Strm)
    plugin._configuration = _configuration()
    plugin._configuration_valid = True
    plugin.update_config = lambda *args, **kwargs: pytest.fail("校验失败不应调用宿主保存")

    with pytest.raises(Exception) as error:
        plugin.save_config(
            {
                "instances": [
                    {
                        "name": "实例",
                        "scan_url": "https://openlist.example.invalid",
                        "api_token": "configured",
                        "mappings": [{"source_dir": "relative", "output_dir": "/strm"}],
                    }
                ]
            },
            _=object(),
        )

    assert getattr(error.value, "status_code", None) == 422


def test_empty_run_scan_body_selects_all_saved_mappings() -> None:
    """联邦页面发送空对象时仍应进入全量扫描入口。"""

    plugin = object.__new__(Alist2Strm)
    plugin.trigger_scan = lambda target=None: {"target": target}

    assert plugin.start_scan({}, _=object()) == {"target": None}


@pytest.mark.anyio
async def test_scan_routes_reject_non_superuser_before_work() -> None:
    """运行全部、单组和状态端点都在业务前拒绝普通用户。"""

    from types import SimpleNamespace

    from fastapi import HTTPException

    plugin = object.__new__(Alist2Strm)
    scan_routes = [item for item in plugin.get_api() if item["path"].startswith("/scan")]
    for route in scan_routes:
        with pytest.raises(HTTPException) as error:
            await _dependency(route["endpoint"])(current_user=SimpleNamespace(is_superuser=False))
        assert error.value.status_code == 403


@pytest.mark.anyio
async def test_superuser_dependency_rejects_non_superuser() -> None:
    """宿主超级管理员依赖应拒绝普通管理用户。"""

    from types import SimpleNamespace

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await get_current_active_superuser_async(current_user=SimpleNamespace(is_superuser=False))

    assert error.value.status_code == 403


@pytest.mark.parametrize("endpoint", ["start_scan", "run_mapping_scan"])
@pytest.mark.parametrize("scan_mode", ["overwrite", None, True, 1, [], {}])
def test_scan_api_rejects_unknown_scan_mode_before_triggering(endpoint: str, scan_mode: object) -> None:
    """扫描方式必须明确可识别，不能把非法强制参数当普通运行。"""

    from fastapi import HTTPException

    plugin = object.__new__(Alist2Strm)
    calls: list[object] = []
    plugin.trigger_scan = lambda target=None: calls.append(target)
    with pytest.raises(HTTPException) as error:
        getattr(plugin, endpoint)(
            {"scan_mode": scan_mode, "target": {"instance_id": "instance-1", "mapping_index": 1}},
            _=object(),
        )
    assert error.value.status_code == 422
    assert not calls


@pytest.mark.parametrize("endpoint", ["start_scan", "run_mapping_scan"])
@pytest.mark.parametrize("nested", [False, True])
def test_force_complete_api_passes_selected_mapping_and_mode(endpoint: str, nested: bool) -> None:
    """两条管理员运行路由将明确方式和解析后的单组目标交给扫描入口。"""

    from app.plugins.alist2strm.scan import ScanTarget  # ty: ignore[unresolved-import]

    plugin = object.__new__(Alist2Strm)
    calls: list[tuple[object, bool]] = []

    def trigger(target: object = None, *, force_complete: bool = False) -> dict[str, object]:
        """捕获运行入口契约而不创建宿主任务。"""

        calls.append((target, force_complete))
        return {"accepted": True}

    plugin.trigger_scan = trigger
    target = {"instance_id": "instance-1", "mapping_index": 2}
    payload = {"scan_mode": "force_complete", "target": target} if nested else {"scan_mode": "force_complete", **target}
    response = getattr(plugin, endpoint)(payload, _=object())
    assert response == {"accepted": True}
    assert calls == [(ScanTarget("instance-1", mapping_index=2), True)]


@pytest.mark.parametrize("endpoint", ["start_scan", "run_mapping_scan"])
@pytest.mark.parametrize(
    "payload",
    [
        {"scan_mode": "force_complete"},
        {"scan_mode": "force_complete", "target": None},
        {"scan_mode": "force_complete", "target": []},
        {"scan_mode": "force_complete", "target": {"instance_id": "instance-1", "mapping_index": True}},
        {"scan_mode": "force_complete", "target": {"instance_id": "instance-1", "mapping_index": 0}},
        {"scan_mode": "force_complete", "target": {"instance_id": "instance-1"}},
        {"scan_mode": "force_complete", "target": {"instance_id": "", "mapping_index": 1}},
    ],
)
def test_force_complete_api_rejects_invalid_target(endpoint: str, payload: dict[str, object]) -> None:
    """强制请求不能省略、批量或使用非法单组选择。"""

    from fastapi import HTTPException

    plugin = object.__new__(Alist2Strm)
    plugin.trigger_scan = lambda *args, **kwargs: pytest.fail("无效目标不能进入任务入口")
    with pytest.raises(HTTPException) as error:
        getattr(plugin, endpoint)(payload, _=object())
    assert error.value.status_code == 422


def test_explicit_normal_scan_without_target_keeps_all_saved_mapping_contract() -> None:
    """显式普通方式仍兼容运行全部已保存映射。"""

    plugin = object.__new__(Alist2Strm)
    plugin.trigger_scan = lambda target=None: {"target": target}
    assert plugin.start_scan({"scan_mode": "normal"}, _=object()) == {"target": None}


@pytest.mark.parametrize("endpoint", ["start_scan", "run_mapping_scan"])
def test_force_complete_api_runs_only_selected_saved_snapshot_and_rejects_duplicate(
    tmp_path: Path, endpoint: str
) -> None:
    """HTTP 参数经真实任务入口捕获保存快照；草稿和重复请求不扩大任务。"""

    from app.plugins.alist2strm.scan import HttpResponse, OpenListClient, ScanCoordinator  # ty: ignore[unresolved-import]

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "saved-instance",
                    "name": "已保存实例",
                    "scan_url": "https://saved.example.invalid",
                    "api_token": "synthetic",
                    "mappings": [
                        {"source_dir": "/one", "output_dir": str(tmp_path / "one")},
                        {"source_dir": "/two", "output_dir": str(tmp_path / "two")},
                    ],
                }
            ],
        }
    )
    calls: list[tuple[str, str]] = []
    jobs: list[Any] = []

    class Transport:
        """OpenList HTTP 边界替身，不允许真实网络。"""

        def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
            """返回指定保存目录的空完整列表。"""

            calls.append((url, payload["path"]))
            return HttpResponse(200, {"code": 200, "data": {"content": [], "total": 0}})

    plugin = object.__new__(Alist2Strm)
    plugin._scan_coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=lambda task: jobs.append(task) or True,
        client_factory=lambda url, token: OpenListClient(url, token, transport=Transport()),
    )
    payload = {"scan_mode": "force_complete", "target": {"instance_id": "saved-instance", "mapping_index": 2}}
    assert getattr(plugin, endpoint)(payload, _=object())["accepted"] is True
    assert plugin.run_mapping_scan(payload, _=object())["accepted"] is False
    assert len(jobs) == 1
    assert plugin.scan_invocations == 1
    configuration.instances[0].scan_url = "https://changed.example.invalid"
    configuration.instances[0].mappings[1].source_dir = "/changed"
    jobs.pop()()
    assert plugin.scan_status(_=object())["state"] == "success"
    assert plugin.scan_status(_=object())["total_mappings"] == 1
    assert calls == [("https://saved.example.invalid/api/fs/list", "/two")]


@pytest.mark.parametrize("endpoint", ["start_scan", "run_mapping_scan"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("explicit_normal", [False, True])
def test_normal_scan_api_preserves_legacy_and_nested_mapping_targets(
    endpoint: str, nested: bool, explicit_normal: bool
) -> None:
    """省略或显式普通方式都保留旧平铺目标及嵌套目标的单映射契约。"""

    from app.plugins.alist2strm.scan import ScanTarget  # ty: ignore[unresolved-import]

    plugin = object.__new__(Alist2Strm)
    calls: list[tuple[object, bool]] = []

    def trigger(target: object = None, *, force_complete: bool = False) -> dict[str, object]:
        """捕获边界解析结果，不创建宿主任务。"""

        calls.append((target, force_complete))
        return {"accepted": True}

    plugin.trigger_scan = trigger
    target = {"instance_id": "instance-1", "mapping_index": 2}
    payload = {"target": target} if nested else target.copy()
    if explicit_normal:
        payload["scan_mode"] = "normal"
    assert getattr(plugin, endpoint)(payload, _=object()) == {"accepted": True}
    assert calls == [(ScanTarget("instance-1", mapping_index=2), False)]
