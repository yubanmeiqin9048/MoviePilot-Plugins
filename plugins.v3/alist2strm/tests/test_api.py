"""插件管理 API 的认证与保存契约测试。"""

from __future__ import annotations

import inspect
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
