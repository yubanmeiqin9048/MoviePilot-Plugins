"""配置契约的行为测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.plugins.alist2strm.config import (  # ty: ignore[unresolved-import]
    OUTPUT_DIRECTORY_WARNING,
    ConfigurationValidationError,
    build_configuration,
    public_configuration,
)


def _instance(name: str, output_dir: str, *, source_dir: str = "/library") -> dict[str, object]:
    """构造不含真实凭据的配置样例。"""

    return {
        "name": name,
        "scan_url": "https://openlist.example.invalid",
        "api_token": "configured",
        "mappings": [{"source_dir": source_dir, "output_dir": output_dir}],
    }


def test_valid_configuration_supports_multiple_instances_and_overlapping_sources() -> None:
    """多个实例、多个映射和源目录包含关系应保留，播放地址为空时回退扫描地址。"""

    snapshot = build_configuration(
        {
            "instances": [
                {
                    "id": "primary",
                    **_instance("主实例", "/strm/primary", source_dir="/library"),
                    "mappings": [
                        {"source_dir": "/library", "output_dir": "/strm/primary"},
                        {"source_dir": "/library/movies", "output_dir": "/strm/primary-movies"},
                    ],
                },
                _instance("备用实例", "/strm/backup", source_dir="/other"),
            ],
            "schedule_enabled": False,
            "history_retention_days": 30,
        }
    )

    assert [item.name for item in snapshot.instances] == ["主实例", "备用实例"]
    assert snapshot.instances[0].playback_url is None
    assert snapshot.instances[0].effective_playback_url == snapshot.instances[0].scan_url
    assert snapshot.instances[0].mappings[0].source_dir == "/library"
    assert snapshot.instances[0].mappings[1].source_dir == "/library/movies"
    assert snapshot.history_retention_days == 30
    assert public_configuration(snapshot)["instances"][0]["api_token_configured"] is True
    assert "api_token" not in public_configuration(snapshot)["instances"][0]


def test_malformed_configuration_is_rejected_before_persistence() -> None:
    """缺少绝对目录或扫描地址时应在进入保存层前失败。"""

    with pytest.raises(ConfigurationValidationError) as error:
        build_configuration(
            {
                "instances": [
                    {
                        "name": "实例",
                        "scan_url": "not-a-url",
                        "api_token": "configured",
                        "mappings": [{"source_dir": "relative", "output_dir": "/strm"}],
                    }
                ]
            }
        )

    assert error.value.code == "invalid_configuration"
    assert "绝对路径" in str(error.value) or "地址" in str(error.value)


def test_output_directory_conflict_reports_both_mapping_labels() -> None:
    """跨实例相等或父子输出目录必须拒绝并指出两条映射。"""

    payload = {
        "instances": [
            _instance("电影实例", "/strm/media"),
            _instance("剧集实例", "/strm/media/tv"),
        ]
    }

    with pytest.raises(ConfigurationValidationError) as error:
        build_configuration(payload)

    assert error.value.code == "output_directory_conflict"
    detail = error.value.as_detail()
    assert len(detail["conflicts"]) == 1
    conflict = detail["conflicts"][0]
    assert conflict["first_instance"] == "电影实例"
    assert conflict["second_instance"] == "剧集实例"
    assert "/strm/media" in str(error.value)
    assert OUTPUT_DIRECTORY_WARNING.endswith("请使用专用输出目录。")


def test_pydantic_models_do_not_silently_accept_unknown_mapping_fields() -> None:
    """拼写错误的映射字段不能静默进入后续扫描配置。"""

    with pytest.raises((ConfigurationValidationError, ValidationError)):
        build_configuration(
            {
                "instances": [
                    {
                        **_instance("实例", "/strm"),
                        "mappings": [{"source_dir": "/library", "outpu_dir": "/strm"}],
                    }
                ]
            }
        )
