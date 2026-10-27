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


def test_active_interval_and_legacy_configuration_migration() -> None:
    """旧 Cron 和文件年龄参数停用，保留开关并应用新默认值。"""

    configuration = build_configuration(
        {"schedule_enabled": True, "schedule_cron": "invalid legacy cron", "time_pruning": {"recent_days": 900}}
    )
    public = public_configuration(configuration)
    assert public["schedule_enabled"] is True
    assert public["active_recheck_days"] == 1
    assert "active_recheck_minutes" not in public
    assert "schedule_cron" not in public and "time_pruning" not in public


@pytest.mark.parametrize("minutes", [1, 15, 60, 1440])
def test_legacy_minutes_migrate_to_one_day(minutes: int) -> None:
    """旧分钟配置统一迁移为一天，显式新配置优先。"""

    assert build_configuration({"active_recheck_minutes": minutes}).active_recheck_days == 1
    configuration = build_configuration({"active_recheck_minutes": minutes, "active_recheck_days": 2})
    assert public_configuration(configuration)["active_recheck_days"] == 2


@pytest.mark.parametrize("value", [0, -1, 1441, 1.5, "60", True])
def test_active_interval_requires_bounded_integer_minutes(value: object) -> None:
    """旧分钟配置仍拒绝原范围之外或类型无效的值。"""

    with pytest.raises(ConfigurationValidationError):
        build_configuration({"active_recheck_minutes": value})


@pytest.mark.parametrize("value", [0, -1, 1.5, "1", True, None])
def test_recent_interval_requires_positive_integer_days(value: object) -> None:
    """近期目录间隔只接受正整数天数。"""

    with pytest.raises(ConfigurationValidationError):
        build_configuration({"active_recheck_days": value})


def test_recent_interval_cannot_exceed_stale_interval() -> None:
    """近期目录不能比陈旧目录复查得更慢。"""

    assert build_configuration({"active_recheck_days": 7}).active_recheck_days == 7
    with pytest.raises(ConfigurationValidationError):
        build_configuration({"active_recheck_days": 8})


@pytest.mark.parametrize(
    "field",
    [
        "recent_days",
        "cold_after_days",
        "archive_after_days",
        "stale_recheck_days",
        "cold_recheck_days",
        "archive_recheck_days",
        "uncertain_recheck_days",
    ],
)
@pytest.mark.parametrize("value", [0, -1, 1.5, "7", True])
def test_file_age_policy_requires_positive_integer_days(field: str, value: object) -> None:
    """所有年龄策略字段拒绝非正整数和隐式类型转换。"""

    with pytest.raises(ConfigurationValidationError):
        build_configuration({"file_age_policy": {field: value}})


@pytest.mark.parametrize(
    "policy",
    [
        {"recent_days": 90},
        {"archive_after_days": 90},
        {"stale_recheck_days": 31},
        {"cold_recheck_days": 91},
    ],
)
def test_file_age_policy_rejects_invalid_order(policy: dict[str, int]) -> None:
    """年龄严格递增，正常档位的复查间隔不得随陈旧程度增加而缩短。"""

    with pytest.raises(ConfigurationValidationError):
        build_configuration({"file_age_policy": policy})
