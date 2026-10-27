"""目录观测 PluginData 恢复及绝对时间的必要边界契约。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from app.plugins.alist2strm.observations import (  # ty: ignore[unresolved-import]
    DirectoryObservationStore,
    parse_modified_time,
)


@pytest.mark.parametrize(
    "corruption", ["missing", "malformed-fingerprint", "malformed-time", "malformed-child", "malformed-record"]
)
def test_observations_recover_valid_siblings_when_local_record_is_corrupt(corruption: str) -> None:
    """损坏一层按未知恢复，不把损坏判空，也不丢失有效兄弟记录。"""

    persisted: dict[str, Any] = {}
    identity = ("instance", "/library", "/output")
    store = DirectoryObservationStore(lambda: None, lambda value: persisted.update(deepcopy(value)))
    store.record_success(identity, "/library", ("/library/a", "/library/b"), 100.0)
    store.record_success(identity, "/library/a", (), 101.0)
    store.record_success(identity, "/library/b", (), 102.0)
    store.complete_cleanup(identity, ("/library", "/library/a", "/library/b"))
    # 此测试专门验证持久化解码契约；扫描测试不依赖序列化布局。
    directories = persisted["mappings"][0]["directories"]
    if corruption == "missing":
        del directories["/library/a"]
    elif corruption == "malformed-fingerprint":
        directories["/library/a"]["fingerprint"] = []
    elif corruption == "malformed-time":
        directories["/library/a"]["successful_at"] = 10**1000
    elif corruption == "malformed-child":
        directories["/library/a"]["children"] = [["not a path"]]
    else:
        directories["/library/a"] = None
    recovered = DirectoryObservationStore(lambda: persisted, lambda _value: None)
    assert recovered.directory(identity, "/library/a").successful_at is None
    assert recovered.directory(identity, "/library/a").needs_check is True
    assert recovered.directory(identity, "/library/b").successful_at == 102.0
    assert recovered.directory(identity, "/library/b").needs_check is False
    recovered.record_success(identity, "/library/a", (), 200.0)
    assert recovered.directory(identity, "/library/a").successful_at == 200.0


def test_observations_reject_self_referential_root_record() -> None:
    """损坏的根目录不能以自身为子目录，且不拖累有效后代的恢复。"""

    persisted: dict[str, Any] = {}
    identity = ("instance", "/", "/output")
    store = DirectoryObservationStore(lambda: None, lambda value: persisted.update(deepcopy(value)))
    store.record_success(identity, "/", ("/a",), 100.0)
    store.record_success(identity, "/a", (), 101.0)
    store.complete_cleanup(identity, ("/", "/a"))
    persisted["mappings"][0]["directories"]["/"]["children"] = ["/"]

    recovered = DirectoryObservationStore(lambda: persisted, lambda _value: None)
    assert recovered.directory(identity, "/").successful_at is None
    assert recovered.directory(identity, "/").needs_check is True
    assert recovered.directory(identity, "/a").successful_at == 101.0
    assert recovered.directory(identity, "/a").needs_check is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-02-01T00:00:00Z", 1_706_745_600.0),
        ("2024-02-01T08:00:00+08:00", 1_706_745_600.0),
        ("2024-02-01T00:00:00", None),
        ("not a time", None),
        (None, None),
        (True, None),
        (1_706_745_600, None),
    ],
)
def test_modified_time_is_absolute_or_unknown(value: object, expected: float | None) -> None:
    """仅接受带时区时间，绝不猜测缺失时区、时间戳单位或当前时间。"""

    assert parse_modified_time(value) == expected


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("stable_seconds", -1),
        ("stable_seconds", float("nan")),
        ("attempted_at", True),
        ("cleanup_retry_at", "tomorrow"),
        ("configuration_revision", -1),
        ("outputs", ["../outside.strm"]),
        ("fingerprint", []),
    ],
)
def test_corrupt_stability_or_cleanup_state_requires_rebuild(field: str, invalid: object) -> None:
    """损坏的稳定或清理状态不能让目录长期跳过检查或越界清理。"""

    persisted: dict[str, Any] = {}
    identity = ("instance", "/library", "/output")
    store = DirectoryObservationStore(lambda: None, lambda value: persisted.update(deepcopy(value)))
    store.record_success(identity, "/library", (), 100.0, 3700.0, fingerprint="known", outputs=("film.strm",))
    persisted["mappings"][0]["directories"]["/library"][field] = invalid
    recovered = DirectoryObservationStore(lambda: persisted, lambda _value: None)
    observation = recovered.directory(identity, "/library")
    assert observation.successful_at is None
    assert observation.needs_check
    assert not observation.outputs


def test_malformed_version_does_not_break_observation_recovery() -> None:
    """损坏的版本字段按无可复用观测处理，不让调度抛出异常。"""

    store = DirectoryObservationStore(lambda: {"version": []}, lambda _value: None)
    assert store.directory(("instance", "/library", "/output"), "/library").needs_check
