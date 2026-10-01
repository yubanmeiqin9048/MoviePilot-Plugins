"""公共 schema 基础契约测试。"""

from datetime import UTC
from uuid import UUID

import pytest
from app.plugins.subtitleassistant.schemas import __all__ as schema_exports
from app.plugins.subtitleassistant.schemas.base import StrictModel, new_id, utc_now
from app.plugins.subtitleassistant.schemas.http import __all__ as http_exports
from app.plugins.subtitleassistant.schemas.http.base import ApiModel
from pydantic import ValidationError


class _SchemaProbe(StrictModel):
    """验证公共 schema 基类配置的最小模型。"""

    count: int


class _HttpProbe(ApiModel):
    """验证 HTTP schema 基类配置的最小模型。"""

    count: int


def test_schema_package_roots_do_not_flatten_exports() -> None:
    """两个 schema 根入口不公开叶契约。"""

    assert schema_exports == []
    assert http_exports == []


def test_schema_base_is_strict_and_rejects_unknown_fields() -> None:
    """公共 schema 基类拒绝隐式类型转换和未知字段。"""

    assert _SchemaProbe(count=1).count == 1
    with pytest.raises(ValidationError):
        _SchemaProbe(count="1")
    with pytest.raises(ValidationError):
        _SchemaProbe(count=1, extra=True)


def test_http_base_is_strict_and_supports_attribute_projection() -> None:
    """HTTP schema 基类保持严格校验并支持显式属性投影。"""

    class ValueObject:
        count = 1

    assert _HttpProbe.model_validate(ValueObject()).count == 1
    with pytest.raises(ValidationError):
        _HttpProbe(count="1")


def test_schema_helpers_provide_utc_time_and_uuid4_ids() -> None:
    """schema 默认值工厂提供带 UTC 时区的时间和 UUIDv4 标识。"""

    assert utc_now().tzinfo is UTC
    assert UUID(new_id()).version == 4
