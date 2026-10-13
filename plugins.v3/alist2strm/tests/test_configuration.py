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
from app.sdk.config import settings


def _instance(name: str, output_dir: str, *, source_dir: str = "/library") -> dict[str, object]:
    """构造不含真实凭据的配置样例。"""

    return {
        "name": name,
        "scan_url": "https://openlist.example.invalid",
        "api_token": "configured",
        "mappings": [{"source_dir": source_dir, "output_dir": output_dir}],
    }


def test_follow_host_defaults_refresh_and_custom_whitelist_restores_follow_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认模式读取宿主当前媒体和字幕后缀，自定义值保持且恢复默认后重新跟随。"""

    monkeypatch.setattr(settings, "RMT_MEDIAEXT", [".MKV"])
    monkeypatch.setattr(settings, "RMT_SUBEXT", [".SRT"])
    payload = {
        "instances": [
            {
                "id": "filters",
                "name": "筛选实例",
                "scan_url": "https://openlist.example.invalid",
                "api_token": "test-token",
                "mappings": [{"source_dir": "/library", "output_dir": "/strm"}],
            }
        ]
    }

    follow = build_configuration(payload)
    assert public_configuration(follow)["instances"][0]["mappings"][0]["effective_extensions"] == [".mkv", ".srt"]

    monkeypatch.setattr(settings, "RMT_MEDIAEXT", [".MP4"])
    monkeypatch.setattr(settings, "RMT_SUBEXT", [".ASS"])
    assert public_configuration(follow)["instances"][0]["mappings"][0]["effective_extensions"] == [".mp4", ".ass"]

    custom_payload = {
        **payload,
        "instances": [
            {
                **payload["instances"][0],
                "mappings": [
                    {
                        "source_dir": "/library",
                        "output_dir": "/strm",
                        "whitelist_mode": "custom",
                        "extensions": [".NFO"],
                    }
                ],
            }
        ],
    }
    custom = build_configuration(custom_payload)
    assert public_configuration(custom)["instances"][0]["mappings"][0]["effective_extensions"] == [".nfo"]

    restored_payload = {
        **custom_payload,
        "instances": [
            {
                **custom_payload["instances"][0],
                "mappings": [
                    {
                        "source_dir": "/library",
                        "output_dir": "/strm",
                        "whitelist_mode": "follow_host",
                        "extensions": [".NFO"],
                    }
                ],
            }
        ],
    }
    restored = build_configuration(restored_payload)
    assert restored.instances[0].mappings[0].extensions == [".nfo"]
    assert public_configuration(restored)["instances"][0]["mappings"][0]["effective_extensions"] == [".mp4", ".ass"]


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


def test_time_pruning_defaults_and_custom_settings_round_trip() -> None:
    """旧配置沿用既有时间规则，自定义七个参数进入公开投影并可重新载入。"""

    assert public_configuration(build_configuration({}))["time_pruning"] == {
        "recent_days": 30,
        "cold_after_days": 90,
        "archive_after_days": 365,
        "stale_recheck_days": 7,
        "cold_recheck_days": 30,
        "archive_recheck_days": 90,
        "empty_recheck_days": 7,
    }
    custom = {
        "recent_days": 5,
        "cold_after_days": 12,
        "archive_after_days": 20,
        "stale_recheck_days": 2,
        "cold_recheck_days": 4,
        "archive_recheck_days": 6,
        "empty_recheck_days": 3,
    }
    public = public_configuration(build_configuration({"time_pruning": custom}))
    assert public["time_pruning"] == custom
    assert build_configuration(public).time_pruning.model_dump() == custom


@pytest.mark.parametrize(
    "field",
    [
        "recent_days",
        "cold_after_days",
        "archive_after_days",
        "stale_recheck_days",
        "cold_recheck_days",
        "archive_recheck_days",
        "empty_recheck_days",
    ],
)
@pytest.mark.parametrize("value", [0, -1, 1.5, "7", True])
def test_time_pruning_requires_positive_integer_days(field: str, value: object) -> None:
    """任一时间参数的非法类型或非正整数都在保存前拒绝。"""

    with pytest.raises(ConfigurationValidationError) as error:
        build_configuration({"time_pruning": {field: value}})
    assert any(item["field"] == f"time_pruning.{field}" for item in error.value.errors)


@pytest.mark.parametrize(
    "overrides",
    [
        {"cold_after_days": 30},
        {"archive_after_days": 90},
        {"cold_recheck_days": 6},
        {"archive_recheck_days": 29},
    ],
)
def test_time_pruning_rejects_unordered_thresholds_and_decreasing_intervals(overrides: dict[str, int]) -> None:
    """阈值重叠和越陈旧越频繁的复查配置应明确拒绝。"""

    with pytest.raises(ConfigurationValidationError):
        build_configuration({"time_pruning": overrides})
