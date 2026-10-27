"""手动扫描高层入口的 OpenList、文件系统和任务控制契约测试。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.plugins.alist2strm import Alist2Strm  # ty: ignore[unresolved-import]
from app.plugins.alist2strm import scan as scan_module  # ty: ignore[unresolved-import]
from app.plugins.alist2strm.config import (  # ty: ignore[unresolved-import]
    ConfigurationValidationError,
    PluginConfiguration,
    build_configuration,
)
from app.plugins.alist2strm.observations import DirectoryObservationStore  # ty: ignore[unresolved-import]
from app.plugins.alist2strm.scan import (  # ty: ignore[unresolved-import]
    DownloadResponse,
    HttpResponse,
    LocalFileSystem,
    OpenListClient,
    OpenListError,
    RemoteEntry,
    ScanCoordinator,
    ScanTarget,
    build_playback_url,
)


class _Transport:
    """按目录树分页返回受控 OpenList 响应，并记录认证边界。"""

    def __init__(self, trees: dict[str, dict[str, list[dict[str, Any]]]], *, null_empty_content: bool = False) -> None:
        """保存多个实例的目录树，并可让空目录返回 OpenList 的 null content。"""

        self.trees = trees
        self.null_empty_content = null_empty_content
        self.calls: list[dict[str, Any]] = []
        self.stream_factory: Any = None

    def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        """模拟 fs/list，不访问真实网络。"""

        self.calls.append({"url": url, "payload": deepcopy(payload), "headers": dict(headers)})
        base = url.rsplit("/api/fs/list", 1)[0]
        tree = self.trees.get(base)
        if tree is None:
            return HttpResponse(404, {"code": 404, "data": None})
        path = str(payload["path"])
        entries = tree.get(path)
        if entries is None:
            return HttpResponse(200, {"code": 500, "data": None})
        page = int(payload["page"])
        per_page = int(payload["per_page"])
        start = (page - 1) * per_page
        content: object = None if self.null_empty_content and not entries else entries[start : start + per_page]
        return HttpResponse(
            200,
            {
                "code": 200,
                "data": {"content": content, "total": len(entries)},
            },
        )

    def get_stream(self, url: str, headers: dict[str, str]) -> DownloadResponse:
        """模拟无签名附件下载，并记录请求认证头。"""

        self.calls.append({"url": url, "headers": dict(headers), "payload": {}})
        if callable(self.stream_factory):
            return self.stream_factory(url, headers)
        return DownloadResponse(status_code=200, body=(f"download:{url}".encode(),), url=url)

    def pages(self, path: str) -> list[int]:
        """返回目录请求的页码。"""

        return [
            int(call["payload"]["page"])
            for call in self.calls
            if "path" in call["payload"] and call["payload"]["path"] == path
        ]


class _Queue:
    """捕获后台任务，测试显式决定何时执行。"""

    def __init__(self) -> None:
        """初始化空任务队列。"""

        self.jobs: list[Any] = []

    def __call__(self, task: Any) -> bool:
        """接收一次宿主调度提交。"""

        self.jobs.append(task)
        return True

    def run(self) -> None:
        """执行当前唯一任务。"""

        jobs, self.jobs = self.jobs, []
        for job in jobs:
            job()


def _config(tmp_path: Path) -> object:
    """构造多实例、多映射的已校验配置快照。"""

    return build_configuration(
        {
            "instances": [
                {
                    "id": "a",
                    "name": "实例 A",
                    "scan_url": "https://scan-a.example.invalid",
                    "playback_url": "https://play-a.example.invalid/",
                    "api_token": "token-a",
                    "mappings": [
                        {
                            "source_dir": "/media/movies",
                            "output_dir": str(tmp_path / "a"),
                        },
                        {
                            "source_dir": "/media/empty",
                            "output_dir": str(tmp_path / "empty"),
                        },
                    ],
                },
                {
                    "id": "b",
                    "name": "实例 B",
                    "scan_url": "https://scan-b.example.invalid",
                    "api_token": "token-b",
                    "mappings": [{"source_dir": "/other", "output_dir": str(tmp_path / "b")}],
                },
            ]
        }
    )


def test_scan_entry_generates_relative_video_strm_with_pages_and_downloads_attachment(tmp_path: Path) -> None:
    """多实例、多映射、深层路径和分页生成视频 STRM 并下载白名单附件。"""

    trees = {
        "https://scan-a.example.invalid": {
            "/media/movies": [
                {"name": "Film.mkv", "is_dir": False},
                {"name": "notes.srt", "is_dir": False},
                {"name": "Season", "is_dir": True},
                {"name": "Handmade.MP4", "is_dir": False},
                {"name": "Ignored.txt", "is_dir": False},
            ],
            "/media/movies/Season": [{"name": "S01", "is_dir": True}],
            "/media/movies/Season/S01": [{"name": "剧集 % ? #.MP4", "is_dir": False}],
            "/media/empty": [],
        },
        "https://scan-b.example.invalid": {
            "/other": [{"name": "Movie.MKV", "is_dir": False}],
        },
    }
    transport = _Transport(trees)
    client = lambda url, token: OpenListClient(url, token, transport=transport, per_page=2)
    queue = _Queue()
    configuration = _config(tmp_path)
    existing = tmp_path / "a" / "Handmade.strm"
    existing.parent.mkdir(parents=True)
    existing.write_text("hand made", encoding="utf-8")
    orphan = tmp_path / "a" / "orphan.strm"
    orphan.write_text("keep", encoding="utf-8")
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=client,
        media_suffix_provider=lambda: (".mkv", ".mp4"),
    )

    result = coordinator.trigger()
    assert result.as_dict() == {"accepted": True, "message": "扫描已在后台启动"}
    queue.run()

    assert (tmp_path / "a" / "Film.strm").read_text(encoding="utf-8") == (
        "https://play-a.example.invalid/d/media/movies/Film.mkv"
    )
    deep = tmp_path / "a" / "Season" / "S01" / "剧集 % ? #.strm"
    assert deep.read_text(encoding="utf-8") == (
        "https://play-a.example.invalid/d/media/movies/Season/S01/%E5%89%A7%E9%9B%86%20%25%20%3F%20%23.MP4"
    )
    assert (tmp_path / "b" / "Movie.strm").read_text(encoding="utf-8") == (
        "https://scan-b.example.invalid/d/other/Movie.MKV"
    )
    assert existing.read_text(encoding="utf-8") == "hand made"
    assert not orphan.exists()
    assert (tmp_path / "a" / "notes.srt").read_bytes() == (
        b"download:https://scan-a.example.invalid/d/media/movies/notes.srt"
    )
    assert transport.pages("/media/movies") == [1, 2, 3]

    status = coordinator.status()
    assert status["state"] == "success"
    assert status["current_mapping"] is None
    assert status["counters"] == {
        "generated": 3,
        "downloaded": 1,
        "updated": 0,
        "skipped": 1,
        "deleted": 1,
        "failed": 0,
    }
    assert status["total_mappings"] == 3
    assert status["mappings"][1]["state"] == "success"  # type: ignore[index]
    for call in transport.calls:
        assert call["headers"] == {"Authorization": "token-a"} or call["headers"] == {"Authorization": "token-b"}
        assert "Bearer" not in str(call["headers"])
        assert "token" not in str(call["payload"]).casefold()


@pytest.mark.parametrize("force_complete", [False, True])
def test_playback_address_change_updates_existing_video_target(tmp_path: Path, force_complete: bool) -> None:
    """已保存播放地址变化时，当前映射的已有视频 STRM 应更新。"""

    first = _config(tmp_path)
    replacement = build_configuration(
        {
            "instances": [
                {
                    "id": "a",
                    "name": "实例 A",
                    "scan_url": "https://scan-a.example.invalid",
                    "playback_url": "https://play-new.example.invalid",
                    "api_token": "token-a",
                    "mappings": [
                        {
                            "source_dir": "/media/movies",
                            "output_dir": str(tmp_path / "a"),
                        }
                    ],
                }
            ]
        }
    )
    current = [first]
    queue = _Queue()
    transport = _Transport(
        {
            "https://scan-a.example.invalid": {
                "/media/movies": [{"name": "Film.mkv", "is_dir": False}],
            }
        }
    )
    coordinator = ScanCoordinator(
        lambda: current[0],
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()
    target = tmp_path / "a" / "Film.strm"
    assert target.read_text(encoding="utf-8") == "https://play-a.example.invalid/d/media/movies/Film.mkv"

    current[0] = replacement
    assert coordinator.trigger(ScanTarget("a", mapping_index=1), force_complete=force_complete).accepted is True
    queue.run()

    assert target.read_text(encoding="utf-8") == "https://play-new.example.invalid/d/media/movies/Film.mkv"
    assert coordinator.status()["counters"] == {
        "generated": 0,
        "downloaded": 0,
        "updated": 1,
        "skipped": 0,
        "deleted": 0,
        "failed": 0,
    }


def test_fallback_playback_address_change_updates_existing_video_target(tmp_path: Path) -> None:
    """播放地址留空时，扫描地址变化也应更新已有视频 STRM。"""

    first = build_configuration(
        {
            "instances": [
                {
                    "id": "fallback",
                    "name": "回退实例",
                    "scan_url": "https://scan-old.example.invalid",
                    "api_token": "token-fallback",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path / "fallback")}],
                }
            ]
        }
    )
    replacement = build_configuration(
        {
            "instances": [
                {
                    "id": "fallback",
                    "name": "回退实例",
                    "scan_url": "https://scan-new.example.invalid",
                    "api_token": "token-fallback",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path / "fallback")}],
                }
            ]
        }
    )
    current = [first]
    queue = _Queue()
    transport = _Transport(
        {
            "https://scan-old.example.invalid": {"/library": [{"name": "Movie.mkv", "is_dir": False}]},
            "https://scan-new.example.invalid": {"/library": [{"name": "Movie.mkv", "is_dir": False}]},
        }
    )
    coordinator = ScanCoordinator(
        lambda: current[0],
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()
    target = tmp_path / "fallback" / "Movie.strm"
    assert target.read_text(encoding="utf-8") == "https://scan-old.example.invalid/d/library/Movie.mkv"

    current[0] = replacement
    assert coordinator.trigger().accepted is True
    queue.run()

    assert target.read_text(encoding="utf-8") == "https://scan-new.example.invalid/d/library/Movie.mkv"
    assert coordinator.status()["counters"]["updated"] == 1  # type: ignore[index]


def test_handmade_existing_strm_skips_until_playback_identity_changes(tmp_path: Path) -> None:
    """手工已有 STRM 在普通扫描保留，明确播放地址变化时随当前映射更新。"""

    first = build_configuration(
        {
            "instances": [
                {
                    "id": "handmade",
                    "name": "手工实例",
                    "scan_url": "https://scan-handmade.example.invalid",
                    "playback_url": "https://play-old.example.invalid",
                    "api_token": "token-handmade",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path / "handmade")}],
                }
            ]
        }
    )
    replacement = build_configuration(
        {
            "instances": [
                {
                    "id": "handmade",
                    "name": "手工实例",
                    "scan_url": "https://scan-handmade.example.invalid",
                    "playback_url": "https://play-new.example.invalid",
                    "api_token": "token-handmade",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path / "handmade")}],
                }
            ]
        }
    )
    current = [first]
    output = tmp_path / "handmade"
    target = output / "Movie.strm"
    output.mkdir()
    target.write_text("hand-made destination", encoding="utf-8")
    queue = _Queue()
    transport = _Transport(
        {"https://scan-handmade.example.invalid": {"/library": [{"name": "Movie.mkv", "is_dir": False}]}}
    )
    coordinator = ScanCoordinator(
        lambda: current[0],
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()
    assert target.read_text(encoding="utf-8") == "hand-made destination"
    assert coordinator.status()["counters"]["skipped"] == 1  # type: ignore[index]

    current[0] = replacement
    assert coordinator.trigger().accepted is True
    queue.run()

    assert target.read_text(encoding="utf-8") == "https://play-new.example.invalid/d/library/Movie.mkv"
    assert coordinator.status()["counters"]["updated"] == 1  # type: ignore[index]


def test_playback_update_failure_preserves_original_and_rescan_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """更新原子替换失败时保留原件，后续可重扫完成地址更新。"""

    first = build_configuration(
        {
            "instances": [
                {
                    "id": "recovery",
                    "name": "恢复实例",
                    "scan_url": "https://scan-recovery.example.invalid",
                    "playback_url": "https://play-old.example.invalid",
                    "api_token": "token-recovery",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path / "recovery")}],
                }
            ]
        }
    )
    replacement = build_configuration(
        {
            "instances": [
                {
                    "id": "recovery",
                    "name": "恢复实例",
                    "scan_url": "https://scan-recovery.example.invalid",
                    "playback_url": "https://play-new.example.invalid",
                    "api_token": "token-recovery",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path / "recovery")}],
                }
            ]
        }
    )
    current = [first]
    queue = _Queue()
    transport = _Transport(
        {"https://scan-recovery.example.invalid": {"/library": [{"name": "Movie.mkv", "is_dir": False}]}}
    )
    coordinator = ScanCoordinator(
        lambda: current[0],
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()
    target = tmp_path / "recovery" / "Movie.strm"
    original = target.read_text(encoding="utf-8")
    stale = tmp_path / "recovery" / "stale.strm"
    stale.write_text("must remain after update failure", encoding="utf-8")

    current[0] = replacement

    def fail_replace(_source: Path, _destination: Path) -> None:
        """模拟本地原子替换失败。"""

        raise OSError("synthetic replacement failure")

    monkeypatch.setattr(scan_module.os, "replace", fail_replace)
    assert coordinator.trigger().accepted is True
    queue.run()

    failed_status = coordinator.status()
    assert target.read_text(encoding="utf-8") == original
    assert failed_status["state"] == "failed"
    assert failed_status["counters"]["updated"] == 0  # type: ignore[index]
    assert failed_status["counters"]["failed"] == 1  # type: ignore[index]
    assert failed_status["mappings"][0]["state"] == "failed"  # type: ignore[index]
    assert stale.is_file()

    monkeypatch.undo()
    assert coordinator.trigger().accepted is True
    queue.run()

    recovered_status = coordinator.status()
    assert target.read_text(encoding="utf-8") == "https://play-new.example.invalid/d/library/Movie.mkv"
    assert recovered_status["state"] == "success"
    assert recovered_status["counters"]["updated"] == 1  # type: ignore[index]
    assert recovered_status["counters"]["failed"] == 0  # type: ignore[index]


def test_overlapping_source_roots_keep_each_mapping_relative(tmp_path: Path) -> None:
    """重叠源目录仍按各自映射根保留相对结构。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "overlap",
                    "name": "重叠源实例",
                    "scan_url": "https://scan-overlap.example.invalid",
                    "api_token": "token-overlap",
                    "mappings": [
                        {"source_dir": "/library", "output_dir": str(tmp_path / "root")},
                        {"source_dir": "/library/season", "output_dir": str(tmp_path / "season")},
                    ],
                }
            ]
        }
    )
    transport = _Transport(
        {
            "https://scan-overlap.example.invalid": {
                "/library": [
                    {"name": "Top.MKV", "is_dir": False},
                    {"name": "season", "is_dir": True},
                ],
                "/library/season": [{"name": "Episode.mkv", "is_dir": False}],
            }
        }
    )
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()

    assert (tmp_path / "root" / "Top.strm").is_file()
    assert (tmp_path / "root" / "season" / "Episode.strm").is_file()
    assert (tmp_path / "season" / "Episode.strm").is_file()
    assert coordinator.status()["counters"]["generated"] == 3  # type: ignore[index]


def test_scan_uses_empty_directories_as_success_and_isolates_bad_mapping(tmp_path: Path) -> None:
    """空目录成功；一个实例/映射失败不阻断其他可用映射。"""

    configuration = _config(tmp_path)
    trees = {
        "https://scan-a.example.invalid": {
            "/media/movies": [{"name": "ok.mkv", "is_dir": False}],
            "/media/empty": [],
        },
        "https://scan-b.example.invalid": {},
    }
    transport = _Transport(trees)
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )
    (tmp_path / "a" / "stale.strm").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "a" / "stale.strm").write_text("stale", encoding="utf-8")
    (tmp_path / "b" / "stale.strm").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "b" / "stale.strm").write_text("stale", encoding="utf-8")

    assert coordinator.trigger().accepted is True
    queue.run()

    status = coordinator.status()
    assert status["state"] == "partial_failure"
    assert status["counters"]["generated"] == 1  # type: ignore[index]
    assert status["counters"]["failed"] == 1  # type: ignore[index]
    assert status["counters"]["deleted"] == 1  # type: ignore[index]
    assert status["mappings"][1]["state"] == "success"  # type: ignore[index]
    assert status["mappings"][2]["state"] == "failed"  # type: ignore[index]
    assert (tmp_path / "a" / "ok.strm").is_file()
    assert not (tmp_path / "a" / "stale.strm").exists()
    assert (tmp_path / "b" / "stale.strm").is_file()
    assert not (tmp_path / "empty").exists()


def test_scan_treats_null_empty_directory_content_as_success(tmp_path: Path) -> None:
    """OpenList 空目录返回 null content 时扫描成功且不累计失败。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "null-empty",
                    "name": "空目录实例",
                    "scan_url": "https://scan-null-empty.example.invalid",
                    "playback_url": "https://play-null-empty.example.invalid",
                    "api_token": "token-null-empty",
                    "mappings": [{"source_dir": "/media/empty", "output_dir": str(tmp_path / "null-empty")}],
                }
            ]
        }
    )
    transport = _Transport(
        {"https://scan-null-empty.example.invalid": {"/media/empty": []}},
        null_empty_content=True,
    )
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()

    status = coordinator.status()
    assert status["state"] == "success"
    assert status["counters"]["failed"] == 0  # type: ignore[index]
    assert status["mappings"][0]["state"] == "success"  # type: ignore[index]
    assert not (tmp_path / "null-empty").exists()


def test_unchanged_playback_rescan_keeps_existing_target_skipped(tmp_path: Path) -> None:
    """有效播放地址未变化时，重复扫描继续跳过已有目标。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "unchanged",
                    "name": "不变实例",
                    "scan_url": "https://scan-unchanged.example.invalid",
                    "playback_url": "https://play-unchanged.example.invalid",
                    "api_token": "token-unchanged",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path / "unchanged")}],
                }
            ]
        }
    )
    queue = _Queue()
    transport = _Transport(
        {"https://scan-unchanged.example.invalid": {"/library": [{"name": "Movie.mkv", "is_dir": False}]}}
    )
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()
    target = tmp_path / "unchanged" / "Movie.strm"
    original = target.read_text(encoding="utf-8")

    assert coordinator.trigger().accepted is True
    queue.run()

    assert target.read_text(encoding="utf-8") == original
    assert coordinator.status()["counters"] == {
        "generated": 0,
        "downloaded": 0,
        "updated": 0,
        "skipped": 1,
        "deleted": 0,
        "failed": 0,
    }


def test_saved_playback_change_waits_until_queued_task_finishes(tmp_path: Path) -> None:
    """任务排队后保存新地址不改变该任务，下次扫描才更新 STRM。"""

    first = build_configuration(
        {
            "instances": [
                {
                    "id": "queued",
                    "name": "排队实例",
                    "scan_url": "https://scan-queued.example.invalid",
                    "playback_url": "https://play-old.example.invalid",
                    "api_token": "token-queued",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path / "queued")}],
                }
            ]
        }
    )
    replacement = build_configuration(
        {
            "instances": [
                {
                    "id": "queued",
                    "name": "排队实例",
                    "scan_url": "https://scan-queued.example.invalid",
                    "playback_url": "https://play-new.example.invalid",
                    "api_token": "token-queued",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path / "queued")}],
                }
            ]
        }
    )
    current = [first]
    queue = _Queue()
    transport = _Transport(
        {"https://scan-queued.example.invalid": {"/library": [{"name": "Movie.mkv", "is_dir": False}]}}
    )
    coordinator = ScanCoordinator(
        lambda: current[0],
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()
    target = tmp_path / "queued" / "Movie.strm"
    original = target.read_text(encoding="utf-8")

    assert coordinator.trigger().accepted is True
    current[0] = replacement
    queue.run()
    queued_status = coordinator.status()
    assert target.read_text(encoding="utf-8") == original
    assert queued_status["counters"]["skipped"] == 1  # type: ignore[index]
    assert queued_status["counters"]["updated"] == 0  # type: ignore[index]

    assert coordinator.trigger().accepted is True
    queue.run()
    assert target.read_text(encoding="utf-8") == "https://play-new.example.invalid/d/library/Movie.mkv"
    assert coordinator.status()["counters"]["updated"] == 1  # type: ignore[index]


def test_duplicate_trigger_skips_and_running_task_keeps_start_snapshot(tmp_path: Path) -> None:
    """重复触发不排队，运行中替换配置也不改变本轮输出。"""

    first = _config(tmp_path)
    replacement = build_configuration(
        {
            "instances": [
                {
                    "id": "a",
                    "name": "替换实例",
                    "scan_url": "https://scan-a.example.invalid",
                    "api_token": "token-a",
                    "mappings": [{"source_dir": "/media/movies", "output_dir": str(tmp_path / "new")}],
                }
            ]
        }
    )
    current = [first]
    queue = _Queue()
    transport = _Transport({"https://scan-a.example.invalid": {"/media/movies": [{"name": "movie.mkv"}]}})
    coordinator = ScanCoordinator(
        lambda: current[0],
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    duplicate = coordinator.trigger()
    assert duplicate.as_dict() == {"accepted": False, "message": "扫描正在运行，已跳过本次触发"}
    assert len(queue.jobs) == 1
    current[0] = replacement
    queue.run()

    assert (tmp_path / "a" / "Film.strm").exists() is False
    assert (tmp_path / "a" / "movie.strm").is_file()
    assert not (tmp_path / "new").exists()


def test_scan_entry_selects_and_downloads_non_video_files(tmp_path: Path) -> None:
    """白名单内的字幕无需同名视频即可原样下载并保留相对路径。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "attachments",
                    "name": "附件实例",
                    "scan_url": "https://scan-attachments.example.invalid",
                    "api_token": "test-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(tmp_path),
                            "whitelist_mode": "custom",
                            "extensions": [".MKV", ".SRT", ".NFO"],
                        }
                    ],
                }
            ]
        }
    )
    transport = _Transport(
        {
            "https://scan-attachments.example.invalid": {
                "/library": [
                    {"name": "Movie.MKV", "is_dir": False},
                    {"name": "orphan.SRT", "is_dir": False},
                    {"name": "metadata.NFO", "is_dir": False},
                    {"name": "ignored.txt", "is_dir": False},
                ]
            }
        }
    )
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
        whitelist_suffix_provider=lambda: (".mkv", ".srt", ".nfo"),
    )

    assert coordinator.trigger().accepted is True
    queue.run()

    assert (tmp_path / "Movie.strm").is_file()
    assert (
        tmp_path / "orphan.SRT"
    ).read_bytes() == b"download:https://scan-attachments.example.invalid/d/library/orphan.SRT"
    assert (tmp_path / "metadata.NFO").is_file()
    assert not (tmp_path / "ignored.txt").exists()
    assert coordinator.status()["counters"] == {
        "generated": 1,
        "downloaded": 2,
        "updated": 0,
        "skipped": 0,
        "deleted": 0,
        "failed": 0,
    }


def test_scan_excludes_matching_directory_subtree_by_name(tmp_path: Path) -> None:
    """目录名称命中排除关键字时不读取其任意深度的子树。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "exclude",
                    "name": "排除实例",
                    "scan_url": "https://scan-exclude.example.invalid",
                    "api_token": "test-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(tmp_path),
                            "whitelist_mode": "custom",
                            "extensions": [".mkv", ".srt"],
                            "exclude_keywords": "SaMpLe",
                        }
                    ],
                }
            ]
        }
    )
    transport = _Transport(
        {
            "https://scan-exclude.example.invalid": {
                "/library": [
                    {"name": "Sample Directory", "is_dir": True},
                    {"name": "keep.MKV", "is_dir": False},
                ],
                "/library/Sample Directory": [{"name": "deep", "is_dir": True}],
                "/library/Sample Directory/deep": [{"name": "hidden.MKV", "is_dir": False}],
            }
        }
    )
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mKv",),
        whitelist_suffix_provider=lambda: (".MKV", ".SRT"),
    )

    assert coordinator.trigger().accepted is True
    queue.run()

    assert (tmp_path / "keep.strm").is_file()
    assert not (tmp_path / "Sample Directory").exists()
    assert transport.pages("/library/Sample Directory") == []
    assert transport.pages("/library/Sample Directory/deep") == []


def test_interrupted_attachment_download_leaves_no_target_and_retries(tmp_path: Path) -> None:
    """下载流中断时只留下失败计数，下一次扫描可以重新创建目标。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "retry",
                    "name": "中断实例",
                    "scan_url": "https://scan-retry.example.invalid",
                    "api_token": "test-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(tmp_path),
                            "whitelist_mode": "custom",
                            "extensions": [".nfo"],
                        }
                    ],
                }
            ]
        }
    )
    transport = _Transport(
        {"https://scan-retry.example.invalid": {"/library": [{"name": "info.nfo", "is_dir": False}]}}
    )
    attempts = 0

    def interrupted_once(url: str, headers: dict[str, str]) -> DownloadResponse:
        """第一次流只返回部分内容后抛出中断。"""

        nonlocal attempts
        attempts += 1
        if attempts == 1:

            def body() -> Any:
                """产生部分正文后中断。"""

                yield b"partial"
                raise OSError("interrupted")

            return DownloadResponse(status_code=200, body=body(), url=url)
        return DownloadResponse(status_code=200, body=(f"download:{url}".encode(),), url=url)

    transport.stream_factory = interrupted_once
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
        whitelist_suffix_provider=lambda: (".nfo",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()
    assert (tmp_path / "info.nfo").read_bytes().startswith(b"download:")
    assert attempts == 2
    assert coordinator.status()["counters"] == {
        "generated": 0,
        "downloaded": 1,
        "updated": 0,
        "skipped": 0,
        "deleted": 0,
        "failed": 0,
    }

    assert coordinator.trigger().accepted is True
    queue.run()
    assert coordinator.status()["counters"] == {
        "generated": 0,
        "downloaded": 0,
        "updated": 0,
        "skipped": 1,
        "deleted": 0,
        "failed": 0,
    }


def test_existing_attachment_target_is_skipped_without_download(tmp_path: Path) -> None:
    """已有附件目标直接跳过，不启动下载或比较内容。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "existing",
                    "name": "已有实例",
                    "scan_url": "https://scan-existing.example.invalid",
                    "api_token": "test-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(tmp_path),
                            "whitelist_mode": "custom",
                            "extensions": [".srt"],
                        }
                    ],
                }
            ]
        }
    )
    target = tmp_path / "subtitle.srt"
    target.write_bytes(b"keep")
    transport = _Transport(
        {"https://scan-existing.example.invalid": {"/library": [{"name": "subtitle.srt", "is_dir": False}]}}
    )
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
        whitelist_suffix_provider=lambda: (".srt",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()

    assert target.read_bytes() == b"keep"
    assert not any(
        "/d/library/subtitle.srt" in call["url"] for call in transport.calls if call["url"].endswith("subtitle.srt")
    )
    assert coordinator.status()["counters"]["skipped"] == 1


def test_same_origin_redirect_drops_authorization() -> None:
    """下载重定向到同源地址后也不携带管理员认证头。"""

    class RedirectTransport:
        """返回同源重定向的下载替身。"""

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def get_stream(self, url: str, headers: dict[str, str]) -> DownloadResponse:
            """首次返回同源重定向，第二次返回正文。"""

            self.calls.append((url, dict(headers)))
            if len(self.calls) == 1:
                return DownloadResponse(
                    status_code=307,
                    headers={"Location": "/redirected/file"},
                    url=url,
                )
            return DownloadResponse(status_code=200, body=(b"file",), url=url)

        def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
            """该测试不使用目录 API。"""

            return HttpResponse(500)

    transport = RedirectTransport()
    client = OpenListClient("https://scan.example.invalid", "test-token", transport=transport)

    with client.open_download("/library/file.nfo") as chunks:
        assert b"".join(chunks) == b"file"

    assert transport.calls[0][1] == {"Authorization": "test-token"}
    assert transport.calls[1][1] == {}
    assert transport.calls[1][0] == "https://scan.example.invalid/redirected/file"


def test_cross_origin_redirect_drops_authorization_and_url_credentials() -> None:
    """下载重定向到代理源时不携带管理员认证头或 URL 用户信息。"""

    class RedirectTransport:
        """返回跨源重定向的下载替身。"""

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def get_stream(self, url: str, headers: dict[str, str]) -> DownloadResponse:
            """首次重定向，第二次返回正文。"""

            self.calls.append((url, dict(headers)))
            if len(self.calls) == 1:
                return DownloadResponse(
                    status_code=302,
                    headers={"Location": "https://user:password@proxy.example.invalid/file"},
                    url=url,
                )
            return DownloadResponse(status_code=200, body=(b"file",), url=url)

        def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
            """该测试不使用目录 API。"""

            return HttpResponse(500)

    transport = RedirectTransport()
    client = OpenListClient("https://scan.example.invalid", "test-token", transport=transport)

    with client.open_download("/library/file.nfo") as chunks:
        assert b"".join(chunks) == b"file"

    assert transport.calls[0][1] == {"Authorization": "test-token"}
    assert transport.calls[1][1] == {}
    assert "user:password" not in transport.calls[1][0]


def test_list_retry_succeeds_after_two_transient_failures_without_material_sleep() -> None:
    """目录页的临时网络失败最多额外重试两次，并可注入零退避。"""

    class RetryTransport:
        """前两次返回服务端错误，第三次返回目录。"""

        def __init__(self) -> None:
            self.attempts = 0

        def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
            """记录请求次数并返回受控响应。"""

            self.attempts += 1
            if self.attempts < 3:
                return HttpResponse(503, {"code": 503, "data": None})
            return HttpResponse(200, {"code": 200, "data": {"content": [], "total": 0}})

    transport = RetryTransport()
    client = OpenListClient(
        "https://retry.example.invalid",
        "test-token",
        transport=transport,
        sleep=lambda _delay: None,
        retry_backoff=lambda _attempt: 0,
    )

    assert client.list_dir("/library") == []
    assert transport.attempts == 3


def test_list_retry_exhaustion_and_non_retryable_responses() -> None:
    """临时失败耗尽为三次；权限、4xx 和无效响应均不重试。"""

    class ResponseTransport:
        """按给定响应顺序返回目录请求结果。"""

        def __init__(self, responses: list[HttpResponse]) -> None:
            self.responses = responses
            self.attempts = 0

        def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
            """返回下一个响应。"""

            self.attempts += 1
            return self.responses[min(self.attempts - 1, len(self.responses) - 1)]

    for response in (
        HttpResponse(401, {"code": 401, "data": None}),
        HttpResponse(403, {"code": 403, "data": None}),
        HttpResponse(404, {"code": 404, "data": None}),
        HttpResponse(200, {"code": 200, "data": None}),
    ):
        transport = ResponseTransport([response])
        client = OpenListClient(
            "https://retry.example.invalid",
            "test-token",
            transport=transport,
            sleep=lambda _delay: None,
            retry_backoff=lambda _attempt: 0,
        )
        with pytest.raises(OpenListError):
            client.list_dir("/library")
        assert transport.attempts == 1

    transport = ResponseTransport([HttpResponse(503, {"code": 503, "data": None})])
    client = OpenListClient(
        "https://retry.example.invalid",
        "test-token",
        transport=transport,
        sleep=lambda _delay: None,
        retry_backoff=lambda _attempt: 0,
    )
    with pytest.raises(OpenListError) as error:
        client.list_dir("/library")
    assert error.value.attempts_exhausted is True
    assert transport.attempts == 3


def test_list_treats_null_content_as_empty_directory() -> None:
    """OpenList 空目录把 content 序列化为 null 时按空列表处理且不重试。"""

    class NullContentTransport:
        """返回 OpenList 空目录的 null content 响应。"""

        def __init__(self) -> None:
            self.attempts = 0

        def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
            """记录请求次数并返回空目录响应。"""

            self.attempts += 1
            return HttpResponse(200, {"code": 200, "data": {"content": None, "total": 0}})

    transport = NullContentTransport()
    client = OpenListClient(
        "https://null.example.invalid",
        "test-token",
        transport=transport,
        sleep=lambda _delay: None,
        retry_backoff=lambda _attempt: 0,
    )

    assert client.list_dir("/library") == []
    assert transport.attempts == 1


def test_download_retry_exhaustion_isolated_from_good_attachment(tmp_path: Path) -> None:
    """附件临时失败耗尽只使该文件失败，其他附件继续处理。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "downloads",
                    "name": "下载实例",
                    "scan_url": "https://downloads.example.invalid",
                    "api_token": "download-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(tmp_path),
                            "whitelist_mode": "custom",
                            "extensions": [".nfo"],
                        }
                    ],
                }
            ]
        }
    )

    class Reader:
        """一个附件耗尽重试、另一个附件成功的目录替身。"""

        def list_dir(self, path: str) -> list[object]:
            """返回两个白名单附件。"""

            return [RemoteEntry("bad.nfo", False), RemoteEntry("good.nfo", False)]

        def open_download(self, path: str) -> Any:
            """只让 bad 附件返回可重试失败。"""

            from contextlib import contextmanager

            @contextmanager
            def opened() -> Any:
                """提供下载流或抛出临时错误。"""

                if path.endswith("bad.nfo"):
                    raise OpenListError("临时下载失败", retryable=True)
                yield (b"good",)

            return opened()

    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: Reader(),
        media_suffix_provider=lambda: (".mkv",),
        sleep=lambda _delay: None,
        retry_backoff=lambda _attempt: 0,
    )

    assert coordinator.trigger().accepted is True
    queue.run()

    assert not (tmp_path / "bad.nfo").exists()
    assert (tmp_path / "good.nfo").read_bytes() == b"good"
    assert coordinator.status()["counters"] == {
        "generated": 0,
        "downloaded": 1,
        "updated": 0,
        "skipped": 0,
        "deleted": 0,
        "failed": 1,
    }
    assert coordinator.status()["state"] == "partial_failure"
    assert coordinator.status()["mappings"][0]["cleanup_eligible"] is False  # type: ignore[index]


def test_local_attachment_write_failure_is_not_retried(tmp_path: Path) -> None:
    """本地原子写入失败不因下载重试策略重复写入。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "local-write",
                    "name": "本地写入实例",
                    "scan_url": "https://local-write.example.invalid",
                    "api_token": "local-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(tmp_path),
                            "whitelist_mode": "custom",
                            "extensions": [".nfo"],
                        }
                    ],
                }
            ]
        }
    )
    writes = 0

    class Reader:
        """返回一个可下载附件。"""

        def list_dir(self, path: str) -> list[RemoteEntry]:
            """返回附件。"""

            return [RemoteEntry("info.nfo", False)]

        def open_download(self, path: str) -> Any:
            """返回完整正文。"""

            from contextlib import contextmanager

            @contextmanager
            def opened() -> Any:
                """提供一个字节块。"""

                yield (b"info",)

            return opened()

    class FileSystem:
        """记录并拒绝本地写入。"""

        def exists(self, target: Path) -> bool:
            """目标不存在。"""

            return False

        def write_text_atomic(self, target: Path, content: str) -> bool:
            """该场景不生成 STRM。"""

            raise AssertionError("不应写入 STRM")

        def replace_text_atomic(self, target: Path, content: str) -> None:
            """该场景不更新 STRM。"""

            raise AssertionError("不应更新 STRM")

        def write_bytes_atomic(self, target: Path, chunks: Any) -> bool:
            """只失败一次即可证明没有重试。"""

            nonlocal writes
            writes += 1
            raise OSError("disk full")

    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: Reader(),
        filesystem=FileSystem(),
        media_suffix_provider=lambda: (".mkv",),
        sleep=lambda _delay: None,
        retry_backoff=lambda _attempt: 0,
    )

    assert coordinator.trigger().accepted is True
    queue.run()
    assert writes == 1
    assert coordinator.status()["counters"]["failed"] == 1  # type: ignore[index]


def test_auth_failure_skips_remaining_mapping_of_instance_but_continues_other_instance(tmp_path: Path) -> None:
    """实例认证失败隔离该实例剩余映射，并继续其他实例。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "denied",
                    "name": "无权实例",
                    "scan_url": "https://denied.example.invalid",
                    "api_token": "denied-token",
                    "mappings": [
                        {"source_dir": "/one", "output_dir": str(tmp_path / "one")},
                        {"source_dir": "/two", "output_dir": str(tmp_path / "two")},
                    ],
                },
                {
                    "id": "allowed",
                    "name": "可用实例",
                    "scan_url": "https://allowed.example.invalid",
                    "api_token": "allowed-token",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path / "allowed")}],
                },
            ]
        }
    )
    calls: list[tuple[str, str]] = []

    class Reader:
        """按实例提供认证失败或正常目录。"""

        def __init__(self, denied: bool) -> None:
            self.denied = denied

        def list_dir(self, path: str) -> list[RemoteEntry]:
            """返回认证错误或一个视频。"""

            calls.append(("denied" if self.denied else "allowed", path))
            if self.denied:
                raise OpenListError("权限不足", retryable=False, status_code=403)
            return [RemoteEntry("ok.mkv", False)]

    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, _token: Reader("denied" in url),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()

    assert calls == [("denied", "/one"), ("allowed", "/library")]
    status = coordinator.status()
    assert status["state"] == "partial_failure"
    assert status["mappings"][1]["state"] == "failed"  # type: ignore[index]
    assert status["mappings"][2]["state"] == "success"  # type: ignore[index]
    assert (tmp_path / "allowed" / "ok.strm").is_file()


def test_stop_finishes_current_atomic_write_then_stops_before_next_file(tmp_path: Path) -> None:
    """停止请求在当前原子写入后生效，后续文件不再开始。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "stop",
                    "name": "停止实例",
                    "scan_url": "https://stop.example.invalid",
                    "api_token": "stop-token",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path)}],
                }
            ]
        }
    )
    queue = _Queue()
    coordinator: ScanCoordinator
    writes: list[str] = []

    class FileSystem:
        """在第一项原子写入完成后发出停止请求。"""

        def exists(self, target: Path) -> bool:
            """返回目标是否存在。"""

            return target.exists()

        def write_text_atomic(self, target: Path, content: str) -> bool:
            """写入第一项后请求停止。"""

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            writes.append(target.name)
            if len(writes) == 1:
                assert coordinator.stop().as_dict() == {"accepted": True, "message": "扫描正在停止"}
            return True

        def replace_text_atomic(self, target: Path, content: str) -> None:
            """该场景不更新既有目标。"""

            raise AssertionError("不应更新既有目标")

        def write_bytes_atomic(self, target: Path, chunks: Any) -> bool:
            """该场景不下载附件。"""

            raise AssertionError("不应下载附件")

    class Reader:
        """返回两个视频，验证第二项不会开始。"""

        def list_dir(self, path: str) -> list[RemoteEntry]:
            """返回两个视频。"""

            return [RemoteEntry("first.mkv", False), RemoteEntry("second.mkv", False)]

    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: Reader(),
        media_suffix_provider=lambda: (".mkv",),
        filesystem=FileSystem(),
    )

    assert coordinator.trigger().accepted is True
    assert coordinator.status()["running"] is True
    queue.run()

    status = coordinator.status()
    assert status["state"] == "stopped"
    assert status["running"] is False
    assert status["counters"]["generated"] == 1  # type: ignore[index]
    assert writes == ["first.strm"]
    assert not (tmp_path / "second.strm").exists()
    assert status["mappings"][0]["cleanup_eligible"] is False  # type: ignore[index]
    assert coordinator.stop().accepted is False


def test_stop_before_queued_job_is_idle_safe_and_next_trigger_is_fresh_scan(tmp_path: Path) -> None:
    """排队任务停止后不执行；下一次触发重新读取完整目录。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "fresh",
                    "name": "重新扫描实例",
                    "scan_url": "https://fresh.example.invalid",
                    "api_token": "fresh-token",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path)}],
                }
            ]
        }
    )
    queue = _Queue()
    reads = 0

    class Reader:
        """记录每次全量扫描的目录读取次数。"""

        def list_dir(self, path: str) -> list[RemoteEntry]:
            """每次返回同一视频。"""

            nonlocal reads
            reads += 1
            return [RemoteEntry("movie.mkv", False)]

    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: Reader(),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    assert coordinator.stop().as_dict() == {"accepted": True, "message": "扫描正在停止"}
    assert coordinator.status()["state"] == "stopped"
    queue.run()
    assert reads == 0

    assert coordinator.trigger().accepted is True
    queue.run()
    assert reads == 1
    assert (tmp_path / "movie.strm").is_file()


def test_scan_shutdown_closes_client_resources(tmp_path: Path) -> None:
    """停止服务时已创建的客户端最终释放资源。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "release",
                    "name": "释放实例",
                    "scan_url": "https://release.example.invalid",
                    "api_token": "release-token",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path / "release-output")}],
                }
            ]
        }
    )
    queue = _Queue()
    released: list[str] = []

    class Reader:
        """提供可观察的 close 资源。"""

        def list_dir(self, path: str) -> list[RemoteEntry]:
            """返回空目录。"""

            return []

        def close(self) -> None:
            """记录关闭。"""

            released.append("closed")

    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: Reader(),
        media_suffix_provider=lambda: (".mkv",),
    )
    assert coordinator.trigger().accepted is True
    queue.run()
    coordinator.shutdown()
    assert released == ["closed"]


def test_playback_url_encodes_each_path_segment_without_double_encoding() -> None:
    """播放基地址只拼一次，特殊字符按段编码且不写入签名。"""

    assert build_playback_url("https://play.example.invalid/", "/A B/%?#/剧.mkv") == (
        "https://play.example.invalid/d/A%20B/%25%3F%23/%E5%89%A7.mkv"
    )
    assert build_playback_url("https://user:password@play.example.invalid/base?token=secret", "/movie.mkv") == (
        "https://play.example.invalid/base/d/movie.mkv"
    )


def test_complete_scan_cleans_current_snapshot_and_protects_other_files(tmp_path: Path) -> None:
    """完整快照清理改名旧产物、排除子树和手工文件，并保护无关位置。"""

    output = tmp_path / "current"
    old_output = tmp_path / "old-location"
    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "cleanup",
                    "name": "清理实例",
                    "scan_url": "https://cleanup.example.invalid",
                    "api_token": "cleanup-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(output),
                            "whitelist_mode": "custom",
                            "extensions": [".mkv", ".srt"],
                            "exclude_keywords": "Sample",
                        }
                    ],
                }
            ]
        }
    )
    output.mkdir(parents=True)
    (output / "Renamed.strm").write_text("hand made", encoding="utf-8")
    (output / "Original.strm").write_text("old source", encoding="utf-8")
    (output / "manual.strm").write_text("manual", encoding="utf-8")
    (output / "stale.srt").write_bytes(b"stale")
    (output / "Sample subtree" / "hidden.strm").parent.mkdir(parents=True)
    (output / "Sample subtree" / "hidden.strm").write_text("excluded", encoding="utf-8")
    (output / "unmanaged.jpg").write_bytes(b"keep")
    (old_output / "Original.strm").parent.mkdir(parents=True)
    (old_output / "Original.strm").write_text("old location", encoding="utf-8")
    transport = _Transport(
        {
            "https://cleanup.example.invalid": {
                "/library": [
                    {"name": "Renamed.mkv", "is_dir": False},
                    {"name": "subtitle.srt", "is_dir": False},
                    {"name": "Sample subtree", "is_dir": True},
                ]
            }
        }
    )
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()

    assert (output / "Renamed.strm").read_text(encoding="utf-8") == "hand made"
    assert (output / "subtitle.srt").is_file()
    assert not (output / "Original.strm").exists()
    assert not (output / "manual.strm").exists()
    assert not (output / "stale.srt").exists()
    assert not (output / "Sample subtree" / "hidden.strm").exists()
    assert (output / "unmanaged.jpg").is_file()
    assert (old_output / "Original.strm").is_file()
    status = coordinator.status()
    assert status["state"] == "success"
    assert status["counters"] == {
        "generated": 0,
        "downloaded": 1,
        "updated": 0,
        "skipped": 1,
        "deleted": 4,
        "failed": 0,
    }
    assert status["mappings"][0]["cleanup_eligible"] is True  # type: ignore[index]


def test_empty_complete_scan_cleans_all_currently_managed_files(tmp_path: Path) -> None:
    """成功的空目录快照也清理当前管理类型并保留无关文件。"""

    output = tmp_path / "empty"
    output.mkdir()
    (output / "stale.strm").write_text("stale", encoding="utf-8")
    (output / "stale.srt").write_bytes(b"stale")
    (output / "unmanaged.txt").write_text("keep", encoding="utf-8")
    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "empty",
                    "name": "空目录实例",
                    "scan_url": "https://empty-cleanup.example.invalid",
                    "api_token": "empty-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(output),
                            "whitelist_mode": "custom",
                            "extensions": [".mkv", ".srt"],
                        }
                    ],
                }
            ]
        }
    )
    queue = _Queue()
    transport = _Transport({"https://empty-cleanup.example.invalid": {"/library": []}})
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()

    assert not (output / "stale.strm").exists()
    assert not (output / "stale.srt").exists()
    assert (output / "unmanaged.txt").is_file()
    status = coordinator.status()
    assert status["state"] == "success"
    assert status["counters"]["deleted"] == 2  # type: ignore[index]
    assert status["mappings"][0]["cleanup_eligible"] is True  # type: ignore[index]


def test_removed_video_suffix_preserves_only_current_strm_and_deletes_stale_strm(tmp_path: Path) -> None:
    """移除一个视频后缀但仍管理另一视频后缀时只保留当前预期 STRM。"""

    output = tmp_path / "video-suffixes"
    first = build_configuration(
        {
            "instances": [
                {
                    "id": "video-suffixes",
                    "name": "视频后缀实例",
                    "scan_url": "https://video-suffixes.example.invalid",
                    "api_token": "video-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(output),
                            "whitelist_mode": "custom",
                            "extensions": [".mkv", ".mp4"],
                        }
                    ],
                }
            ]
        }
    )
    second = build_configuration(
        {
            "instances": [
                {
                    "id": "video-suffixes",
                    "name": "视频后缀实例",
                    "scan_url": "https://video-suffixes.example.invalid",
                    "api_token": "video-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(output),
                            "whitelist_mode": "custom",
                            "extensions": [".mp4"],
                        }
                    ],
                }
            ]
        }
    )
    current = [first]

    class Reader:
        """先返回两个视频，后续只保留仍管理的视频后缀。"""

        def list_dir(self, path: str) -> list[RemoteEntry]:
            """返回当前任务的源快照。"""

            if current[0] is first:
                return [RemoteEntry("Current.mp4", False), RemoteEntry("Removed.mkv", False)]
            return [RemoteEntry("Current.mp4", False)]

    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: current[0],
        dispatch=queue,
        client_factory=lambda _url, _token: Reader(),
        media_suffix_provider=lambda: (".mkv", ".mp4"),
    )
    assert coordinator.trigger().accepted is True
    queue.run()
    assert (output / "Current.strm").is_file()
    assert (output / "Removed.strm").is_file()

    current[0] = second
    assert coordinator.trigger().accepted is True
    queue.run()

    assert (output / "Current.strm").is_file()
    assert not (output / "Removed.strm").exists()
    assert coordinator.status()["counters"]["deleted"] == 1  # type: ignore[index]


def test_removed_suffix_is_preserved_while_strm_management_follows_video_whitelist(tmp_path: Path) -> None:
    """移除附件后缀保留，视频白名单仍在时管理 STRM，退出后保留 STRM。"""

    output = tmp_path / "suffixes"
    first = build_configuration(
        {
            "instances": [
                {
                    "id": "suffixes",
                    "name": "后缀实例",
                    "scan_url": "https://suffixes.example.invalid",
                    "api_token": "suffix-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(output),
                            "whitelist_mode": "custom",
                            "extensions": [".mkv"],
                        }
                    ],
                }
            ]
        }
    )
    second = build_configuration(
        {
            "instances": [
                {
                    "id": "suffixes",
                    "name": "后缀实例",
                    "scan_url": "https://suffixes.example.invalid",
                    "api_token": "suffix-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(output),
                            "whitelist_mode": "custom",
                            "extensions": [".srt"],
                        }
                    ],
                }
            ]
        }
    )
    output.mkdir()
    (output / "video.strm").write_text("managed while video enabled", encoding="utf-8")
    (output / "removed.srt").write_bytes(b"keep after removal")
    transport = _Transport({"https://suffixes.example.invalid": {"/library": []}})
    current = [first]
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: current[0],
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()
    assert not (output / "video.strm").exists()
    assert (output / "removed.srt").is_file()

    (output / "legacy.strm").write_text("strm is no longer managed", encoding="utf-8")
    (output / "stale.srt").write_bytes(b"managed attachment")
    current[0] = second
    assert coordinator.trigger().accepted is True
    queue.run()

    assert (output / "legacy.strm").is_file()
    assert not (output / "removed.srt").exists()
    assert not (output / "stale.srt").exists()
    assert coordinator.status()["counters"]["deleted"] == 2  # type: ignore[index]


@pytest.mark.parametrize("failure", ["directory", "generation", "download", "enumeration"])
def test_incomplete_scan_or_cleanup_enumeration_never_cleans(tmp_path: Path, failure: str) -> None:
    """目录、生成、下载和清理枚举失败均保留既有受管文件。"""

    output = tmp_path / failure
    output.mkdir()
    managed_name = "stale.srt" if failure == "download" else "stale.strm"
    managed = output / managed_name
    managed.write_text("must remain", encoding="utf-8")
    suffix = ".srt" if failure == "download" else ".mkv"
    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": failure,
                    "name": f"失败 {failure}",
                    "scan_url": f"https://{failure}.example.invalid",
                    "api_token": "failure-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(output),
                            "whitelist_mode": "custom",
                            "extensions": [suffix],
                        }
                    ],
                }
            ]
        }
    )

    class Reader:
        """提供一个受控失败或空快照。"""

        def list_dir(self, path: str) -> list[RemoteEntry]:
            """返回当前失败场景的源目录结果。"""

            if failure == "directory":
                raise OpenListError("目录读取失败", retryable=False)
            if failure == "download":
                return [RemoteEntry("attachment.srt", False)]
            if failure == "generation":
                return [RemoteEntry("movie.mkv", False)]
            return []

        def open_download(self, path: str) -> Any:
            """让附件下载在打开阶段失败。"""

            from contextlib import contextmanager

            @contextmanager
            def opened() -> Any:
                """抛出受控下载错误。"""

                raise OpenListError("下载失败", retryable=False)
                yield ()

            return opened()

    class FileSystem(LocalFileSystem):
        """在生成或清理枚举边界注入失败。"""

        def write_text_atomic(self, target: Path, content: str) -> bool:
            """模拟 STRM 创建失败。"""

            if failure == "generation":
                raise OSError("generation failed")
            return super().write_text_atomic(target, content)

        def iter_files(self, root: Path) -> Any:
            """模拟清理候选枚举失败。"""

            if failure == "enumeration":
                raise OSError("enumeration failed")
            return super().iter_files(root)

    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: Reader(),
        filesystem=FileSystem(),
        media_suffix_provider=lambda: (".mkv",),
        sleep=lambda _delay: None,
        retry_backoff=lambda _attempt: 0,
    )

    assert coordinator.trigger().accepted is True
    queue.run()

    status = coordinator.status()
    assert managed.is_file()
    assert status["state"] != "success"
    assert status["mappings"][0]["state"] == "failed"  # type: ignore[index]
    assert status["mappings"][0]["cleanup_eligible"] is False  # type: ignore[index]
    assert status["counters"]["failed"] >= 1  # type: ignore[index]


def test_later_page_failure_preserves_stale_output_and_skips_cleanup(tmp_path: Path) -> None:
    """分页后页失败时保留旧产物并禁止清理。"""

    output = tmp_path / "later-page-failure"
    output.mkdir()
    stale = output / "stale.strm"
    stale.write_text("must remain", encoding="utf-8")
    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "later-page-failure",
                    "name": "后页失败实例",
                    "scan_url": "https://later-page-failure.example.invalid",
                    "api_token": "later-page-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(output),
                            "whitelist_mode": "custom",
                            "extensions": [".mkv"],
                        }
                    ],
                }
            ]
        }
    )

    class Transport(_Transport):
        """只让目录第二页返回不可重试的服务失败。"""

        def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
            """在后页边界返回服务错误。"""

            if payload.get("path") == "/library" and int(payload.get("page", 0)) == 2:
                return HttpResponse(503, {"code": 503, "data": None})
            return super().post_json(url, payload, headers)

    transport = Transport(
        {
            "https://later-page-failure.example.invalid": {
                "/library": [
                    {"name": "new.mkv", "is_dir": False},
                    {"name": "another.mkv", "is_dir": False},
                ]
            }
        }
    )
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport, per_page=1, max_retries=0),
        media_suffix_provider=lambda: (".mkv",),
    )
    assert coordinator.trigger().accepted is True
    queue.run()

    status = coordinator.status()
    assert stale.is_file()
    assert status["state"] == "failed"
    assert status["counters"]["deleted"] == 0  # type: ignore[index]
    assert status["mappings"][0]["cleanup_eligible"] is False  # type: ignore[index]


def test_child_directory_failure_preserves_stale_candidates(tmp_path: Path) -> None:
    """子目录读取最终失败时不清理已有受管产物。"""

    output = tmp_path / "child-failure"
    output.mkdir()
    stale = output / "stale.strm"
    stale.write_text("must remain", encoding="utf-8")
    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "child-failure",
                    "name": "子目录失败实例",
                    "scan_url": "https://child-failure.example.invalid",
                    "api_token": "child-token",
                    "mappings": [{"source_dir": "/library", "output_dir": str(output)}],
                }
            ]
        }
    )

    class Reader:
        """返回一个正常根目录和失败子目录。"""

        def list_dir(self, path: str) -> list[RemoteEntry]:
            """在子目录边界报告失败。"""

            if path == "/library":
                return [RemoteEntry("Season", True)]
            raise OpenListError("子目录读取失败", retryable=False)

    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: Reader(),
        media_suffix_provider=lambda: (".mkv",),
    )
    assert coordinator.trigger().accepted is True
    queue.run()

    assert stale.is_file()
    status = coordinator.status()
    assert status["state"] == "failed"
    assert status["mappings"][0]["state"] == "failed"  # type: ignore[index]
    assert status["mappings"][0]["cleanup_eligible"] is False  # type: ignore[index]


def test_cleanup_never_unlinks_candidate_outside_current_output_root(tmp_path: Path) -> None:
    """注入枚举即使返回根目录外路径，也只能删除当前根内文件。"""

    output = tmp_path / "current"
    outside = tmp_path / "outside.strm"
    output.mkdir()
    inside = output / "inside.strm"
    inside.write_text("inside", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")
    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "boundary",
                    "name": "边界实例",
                    "scan_url": "https://boundary.example.invalid",
                    "api_token": "boundary-token",
                    "mappings": [{"source_dir": "/library", "output_dir": str(output)}],
                }
            ]
        }
    )

    class FileSystem(LocalFileSystem):
        """返回一个越界候选和一个当前根候选。"""

        def iter_files(self, root: Path) -> Any:
            """提供受控候选列表。"""

            return [outside, inside]

    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: _EmptyReader(),
        filesystem=FileSystem(),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()

    assert not inside.exists()
    assert outside.is_file()
    assert coordinator.status()["counters"]["deleted"] == 1  # type: ignore[index]


def test_output_relocation_does_not_clean_old_mapping_root(tmp_path: Path) -> None:
    """源配置迁移到新输出目录时只管理新根并保留旧产物。"""

    old_output = tmp_path / "old-output"
    new_output = tmp_path / "new-output"
    first = build_configuration(
        {
            "instances": [
                {
                    "id": "relocation",
                    "name": "迁移实例",
                    "scan_url": "https://relocation.example.invalid",
                    "api_token": "relocation-token",
                    "mappings": [{"source_dir": "/old", "output_dir": str(old_output)}],
                }
            ]
        }
    )
    second = build_configuration(
        {
            "instances": [
                {
                    "id": "relocation",
                    "name": "迁移实例",
                    "scan_url": "https://relocation.example.invalid",
                    "api_token": "relocation-token",
                    "mappings": [{"source_dir": "/new", "output_dir": str(new_output)}],
                }
            ]
        }
    )
    current = [first]
    transport = _Transport(
        {
            "https://relocation.example.invalid": {
                "/old": [{"name": "Movie.mkv", "is_dir": False}],
                "/new": [],
            }
        }
    )
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: current[0],
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )
    assert coordinator.trigger().accepted is True
    queue.run()
    old_target = old_output / "Movie.strm"
    assert old_target.is_file()

    current[0] = second
    assert coordinator.trigger().accepted is True
    queue.run()

    assert old_target.is_file()
    assert not new_output.exists()
    assert coordinator.status()["state"] == "success"


def test_symlink_aliases_disqualify_selected_cleanup_and_configuration_validation(tmp_path: Path) -> None:
    """符号链接别名配置被拒绝，绕过保存校验时单映射清理仍保守跳过。"""

    real_output = tmp_path / "real-output"
    real_output.mkdir()
    alias_output = tmp_path / "alias-output"
    alias_output.symlink_to(real_output, target_is_directory=True)
    stale = real_output / "stale.strm"
    stale.write_text("must remain", encoding="utf-8")
    payload = {
        "instances": [
            {
                "id": "real",
                "name": "真实目录实例",
                "scan_url": "https://real-output.example.invalid",
                "api_token": "real-token",
                "mappings": [
                    {"source_dir": "/library", "output_dir": str(real_output)},
                    {"source_dir": "/alias", "output_dir": str(alias_output)},
                ],
            }
        ]
    }
    with pytest.raises(ConfigurationValidationError, match="输出目录不能重复或互相包含"):
        build_configuration(payload)

    configuration = PluginConfiguration.model_validate(payload)
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: _EmptyReader(),
        media_suffix_provider=lambda: (".mkv",),
    )
    assert coordinator.trigger(ScanTarget(instance_id="real", mapping_index=1)).accepted is True
    queue.run()

    assert stale.is_file()
    status = coordinator.status()
    assert status["mappings"][0]["cleanup_eligible"] is False  # type: ignore[index]


def test_internal_output_symlink_preserves_expected_strm_by_resolved_identity(tmp_path: Path) -> None:
    """输出根内的目录别名不会让清理删除实际预期 STRM。"""

    output = tmp_path / "output"
    real_directory = output / "real"
    real_directory.mkdir(parents=True)
    alias_directory = output / "alias"
    alias_directory.symlink_to(real_directory, target_is_directory=True)
    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "internal-alias",
                    "name": "内部别名实例",
                    "scan_url": "https://internal-alias.example.invalid",
                    "api_token": "internal-alias-token",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(output),
                            "whitelist_mode": "custom",
                            "extensions": [".mkv"],
                        }
                    ],
                }
            ]
        }
    )

    class Reader:
        """通过输出根内的别名目录返回一个视频。"""

        def list_dir(self, path: str) -> list[RemoteEntry]:
            """返回别名目录和其视频。"""

            if path == "/library":
                return [RemoteEntry("alias", True)]
            return [RemoteEntry("Movie.mkv", False)]

    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: Reader(),
        filesystem=LocalFileSystem(),
        media_suffix_provider=lambda: (".mkv",),
    )
    assert coordinator.trigger().accepted is True
    queue.run()

    target = real_directory / "Movie.strm"
    assert target.is_file()
    status = coordinator.status()
    assert status["state"] == "success"
    assert status["counters"]["generated"] == 1  # type: ignore[index]
    assert status["counters"]["deleted"] == 0  # type: ignore[index]
    assert status["mappings"][0]["cleanup_eligible"] is True  # type: ignore[index]


def test_local_filesystem_child_enumeration_failure_blocks_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地输出根的子目录枚举失败时保留候选并使映射失败。"""

    output = tmp_path / "unreadable-root"
    blocked = output / "blocked"
    blocked.mkdir(parents=True)
    stale = blocked / "stale.strm"
    stale.write_text("must remain", encoding="utf-8")
    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "local-enumeration",
                    "name": "本地枚举实例",
                    "scan_url": "https://local-enumeration.example.invalid",
                    "api_token": "local-token",
                    "mappings": [{"source_dir": "/library", "output_dir": str(output)}],
                }
            ]
        }
    )
    original_scandir = scan_module.os.scandir

    def failing_scandir(path: Any) -> Any:
        """仅让受管输出根的子目录读取失败。"""

        if Path(path) == blocked:
            raise OSError("child directory unreadable")
        return original_scandir(path)

    monkeypatch.setattr(scan_module.os, "scandir", failing_scandir)
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: _EmptyReader(),
        filesystem=LocalFileSystem(),
        media_suffix_provider=lambda: (".mkv",),
    )
    assert coordinator.trigger().accepted is True
    queue.run()

    assert stale.is_file()
    status = coordinator.status()
    assert status["state"] == "failed"
    assert status["mappings"][0]["state"] == "failed"  # type: ignore[index]
    assert status["mappings"][0]["cleanup_eligible"] is False  # type: ignore[index]


class _EmptyReader:
    """清理测试使用的空源目录替身。"""

    def list_dir(self, path: str) -> list[RemoteEntry]:
        """返回空目录。"""

        return []


def test_cleanup_delete_failure_continues_and_marks_run_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单项删除失败后继续删除其他候选并计入失败。"""

    output = tmp_path / "delete-failure"
    output.mkdir()
    first = output / "first.strm"
    second = output / "second.strm"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "delete-failure",
                    "name": "删除失败实例",
                    "scan_url": "https://delete-failure.example.invalid",
                    "api_token": "delete-token",
                    "mappings": [{"source_dir": "/library", "output_dir": str(output)}],
                }
            ]
        }
    )

    class FileSystem(LocalFileSystem):
        """让第一项删除失败而允许后续项完成。"""

        def iter_files(self, root: Path) -> Any:
            """返回稳定顺序的两个候选。"""

            return [first, second]

        def unlink(self, target: Path) -> None:
            """只拒绝第一项。"""

            if target == first:
                raise OSError("unlink failed")
            super().unlink(target)

    queue = _Queue()
    logs: list[str] = []

    class Logger:
        """捕获宿主日志边界。"""

        def warning(self, message: str) -> None:
            """记录一条警告。"""

            logs.append(message)

    monkeypatch.setattr(scan_module.logger, "warning", Logger().warning)
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: _EmptyReader(),
        filesystem=FileSystem(),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()

    status = coordinator.status()
    assert first.is_file()
    assert not second.exists()
    assert status["state"] == "partial_failure"
    assert status["counters"]["deleted"] == 1  # type: ignore[index]
    assert status["counters"]["failed"] == 1  # type: ignore[index]
    assert status["mappings"][0]["state"] == "failed"  # type: ignore[index]
    assert status["mappings"][0]["cleanup_eligible"] is False  # type: ignore[index]
    assert any(
        "受管产物清理失败" in message
        and "删除失败实例" in message
        and "映射 1" in message
        and str(first) in message
        and "unlink failed" in message
        for message in logs
    )


def test_cleanup_stop_finishes_current_unlink_and_blocks_next_candidate(tmp_path: Path) -> None:
    """清理期间停止完成当前删除，不启动下一项且不回滚。"""

    output = tmp_path / "stop-cleanup"
    output.mkdir()
    first = output / "first.strm"
    second = output / "second.strm"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "stop-cleanup",
                    "name": "停止清理实例",
                    "scan_url": "https://stop-cleanup.example.invalid",
                    "api_token": "stop-token",
                    "mappings": [{"source_dir": "/library", "output_dir": str(output)}],
                }
            ]
        }
    )
    coordinator: ScanCoordinator

    class FileSystem(LocalFileSystem):
        """在第一项删除完成后请求停止。"""

        def iter_files(self, root: Path) -> Any:
            """返回稳定顺序的两个候选。"""

            return [first, second]

        def unlink(self, target: Path) -> None:
            """第一项完成后发出停止请求。"""

            super().unlink(target)
            if target == first:
                assert coordinator.stop().as_dict() == {"accepted": True, "message": "扫描正在停止"}

    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: _EmptyReader(),
        filesystem=FileSystem(),
        media_suffix_provider=lambda: (".mkv",),
    )

    assert coordinator.trigger().accepted is True
    queue.run()

    status = coordinator.status()
    assert not first.exists()
    assert second.is_file()
    assert status["state"] == "stopped"
    assert status["counters"]["deleted"] == 1  # type: ignore[index]
    assert status["counters"]["failed"] == 0  # type: ignore[index]
    assert status["mappings"][0]["cleanup_eligible"] is False  # type: ignore[index]


def test_cleanup_stop_does_not_start_subsequent_mapping(tmp_path: Path) -> None:
    """清理停止后不启动后续映射。"""

    first_output = tmp_path / "first-mapping"
    second_output = tmp_path / "second-mapping"
    first_output.mkdir()
    second_output.mkdir()
    first_stale = first_output / "stale.strm"
    second_stale = second_output / "stale.strm"
    first_stale.write_text("first", encoding="utf-8")
    second_stale.write_text("second", encoding="utf-8")
    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "stop-following",
                    "name": "后续映射实例",
                    "scan_url": "https://stop-following.example.invalid",
                    "api_token": "stop-following-token",
                    "mappings": [
                        {"source_dir": "/first", "output_dir": str(first_output)},
                        {"source_dir": "/second", "output_dir": str(second_output)},
                    ],
                }
            ]
        }
    )
    calls: list[str] = []
    coordinator: ScanCoordinator

    class Reader:
        """记录每个映射是否开始读取。"""

        def list_dir(self, path: str) -> list[RemoteEntry]:
            """返回空目录并记录源路径。"""

            calls.append(path)
            return []

    class FileSystem(LocalFileSystem):
        """第一映射清理后请求停止。"""

        def iter_files(self, root: Path) -> Any:
            """返回对应映射的清理候选。"""

            return [first_stale] if root == first_output else [second_stale]

        def unlink(self, target: Path) -> None:
            """完成第一项删除后发出停止请求。"""

            super().unlink(target)
            if target == first_stale:
                assert coordinator.stop().accepted is True

    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: Reader(),
        filesystem=FileSystem(),
        media_suffix_provider=lambda: (".mkv",),
    )
    assert coordinator.trigger().accepted is True
    queue.run()

    assert calls == ["/first"]
    assert not first_stale.exists()
    assert second_stale.is_file()
    status = coordinator.status()
    assert status["state"] == "stopped"
    assert status["mappings"][1]["state"] == "stopped"  # type: ignore[index]


def test_complete_scan_records_fingerprints_and_direct_children(tmp_path: Path) -> None:
    """完整扫描建立本层和深层观测；非白名单参与，目录和排除项时间不参与。"""

    configuration = _config(tmp_path)
    transport = _Transport(
        {
            "https://scan-a.example.invalid": {
                "/media/movies": [
                    {"name": "film.mkv", "is_dir": False, "modified": "2024-01-01T00:00:00Z"},
                    {"name": "notes.txt", "is_dir": False, "modified": "2024-02-01T08:00:00+08:00"},
                    {"name": "Season", "is_dir": True, "modified": "2099-01-01T00:00:00Z"},
                ],
                "/media/movies/Season": [{"name": "Deep", "is_dir": True}],
                "/media/movies/Season/Deep": [
                    {"name": "extra.txt", "is_dir": False, "modified": "2024-03-01T00:00:00Z"}
                ],
            }
        }
    )
    persisted: dict[str, object] = {}
    store = DirectoryObservationStore(
        lambda: persisted.get("value"), lambda value: persisted.__setitem__("value", value)
    )
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport, per_page=2),
        media_suffix_provider=lambda: (".mkv",),
        clock=lambda: 1_720_000_000.0,
        observation_store=store,
    )
    assert coordinator.trigger(ScanTarget("a", mapping_index=1)).accepted
    queue.run()

    identity = ("a", "/media/movies", str(tmp_path / "a"))
    root = store.directory(identity, "/media/movies")
    assert root.fingerprint is not None
    assert root.stable_seconds == 0
    assert root.children == ("/media/movies/Season",)
    assert root.successful_at == 1_720_000_000.0
    assert root.needs_check is False
    assert store.directory(identity, "/media/movies/Season").fingerprint is not None
    assert store.directory(identity, "/media/movies/Season/Deep").fingerprint is not None
    assert transport.pages("/media/movies") == [1, 2]
    assert transport.pages("/media/movies/Season/Deep") == [1]
    assert coordinator.status()["state"] == "success"
    assert (tmp_path / "a" / "film.strm").exists()
    assert not (tmp_path / "a" / "notes.txt").exists()
    recovered = DirectoryObservationStore(lambda: persisted["value"], lambda _value: None)
    assert recovered.directory(identity, "/media/movies") == root
    assert recovered.directory(identity, "/media/movies/Season/Deep") == store.directory(
        identity, "/media/movies/Season/Deep"
    )


def test_single_layer_pruning_skips_remote_listing_and_protects_unscanned_outputs(tmp_path: Path) -> None:
    """单层映射未到复查期限时跳过远端列目录并保护未覆盖产物。"""

    now = [1_720_000_000.0]
    old_modified = now[0] - 40 * 24 * 60 * 60
    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "pruning",
                    "name": "剪枝实例",
                    "scan_url": "https://pruning.example.invalid",
                    "api_token": "test-only-placeholder",
                    "mappings": [{"source_dir": "/library", "output_dir": str(tmp_path)}],
                }
            ]
        }
    )
    transport = _Transport(
        {
            "https://pruning.example.invalid": {
                "/library": [
                    {
                        "name": "Film.mkv",
                        "is_dir": False,
                        "modified": "2024-05-06T00:00:00Z",
                    }
                ]
            }
        }
    )
    # 由独立于生产逻辑的绝对时间构造 40 天前的远端时间。
    transport.trees["https://pruning.example.invalid"]["/library"][0]["modified"] = datetime.fromtimestamp(
        old_modified, tz=UTC
    ).isoformat()
    persisted: dict[str, object] = {}
    store = DirectoryObservationStore(
        lambda: persisted.get("value"), lambda value: persisted.__setitem__("value", value)
    )
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        clock=lambda: now[0],
        observation_store=store,
    )

    assert coordinator.trigger().accepted
    queue.run()
    assert transport.pages("/library") == [1]

    protected = tmp_path / "old.strm"
    protected.write_text("keep while library is unscanned", encoding="utf-8")
    transport.calls.clear()
    now[0] += 3599

    assert coordinator.trigger().accepted
    queue.run()

    assert transport.calls == []
    assert protected.read_text(encoding="utf-8") == "keep while library is unscanned"
    assert coordinator.status()["state"] == "success"


def test_plugin_observations_use_plugindata_and_restart_without_scan(tmp_path: Path) -> None:
    """插件入口按实例保存观测，正常重启恢复事实但不提交任务。"""

    from app.plugins.alist2strm import Alist2Strm  # ty: ignore[unresolved-import]

    persisted: dict[str, object] = {}
    accesses: list[tuple[str, str]] = []
    queue = _Queue()
    transport = _Transport({"https://scan-a.example.invalid": {"/media/movies": []}})

    def plugin() -> Alist2Strm:
        """构造共享 PluginData 的宿主实例，无真实网络。"""

        result = object.__new__(Alist2Strm)
        result._plugin_instance_id = "Alist2Strm-test"
        result._scan_dispatch = queue
        result._scan_clock = lambda: 1_720_000_000.0
        result._scan_client_factory = lambda url, token: OpenListClient(url, token, transport=transport)
        result.get_config = lambda _plugin_id=None: None

        def load(key: str, plugin_id: str) -> object:
            """记录宿主实例分区。"""

            accesses.append((key, plugin_id))
            return persisted.get(key)

        result.get_data = load
        result.save_data = lambda key, value, plugin_id=None: persisted.__setitem__(key, deepcopy(value))
        result.init_plugin(
            {
                "instances": [
                    {
                        "id": "a",
                        "name": "观测实例",
                        "scan_url": "https://scan-a.example.invalid",
                        "api_token": "test-only-placeholder",
                        "mappings": [{"source_dir": "/media/movies", "output_dir": str(tmp_path / "a")}],
                    }
                ]
            }
        )
        return result

    first = plugin()
    assert not queue.jobs
    assert first.trigger_scan()["accepted"] is True
    queue.run()
    identity = ("a", "/media/movies", str(tmp_path / "a"))
    assert first._get_observation_store().directory(identity, "/media/movies").successful_at == 1_720_000_000.0
    restarted = plugin()
    assert not queue.jobs
    assert restarted._get_observation_store().directory(identity, "/media/movies").fingerprint is not None
    assert restarted._get_observation_store().directory(identity, "/media/movies").needs_check is False
    assert (first.OBSERVATIONS_DATA_KEY, "Alist2Strm-test") in accesses
    assert "test-only-placeholder" not in str(persisted[first.OBSERVATIONS_DATA_KEY])


def test_cleanup_failure_retains_only_owner_observation_until_recheck(tmp_path: Path) -> None:
    """删除失败不撤销观测；仅归属目录保留清理要求，到期恢复后完成清理。"""

    configuration = _config(tmp_path)
    output = tmp_path / "a"
    (output / "Season").mkdir(parents=True)
    stale = output / "Season" / "old.strm"
    stale.write_text("keep on failure", encoding="utf-8")
    transport = _Transport(
        {
            "https://scan-a.example.invalid": {
                "/media/movies": [{"name": "Season", "is_dir": True}],
                "/media/movies/Season": [],
            }
        }
    )
    persisted: dict[str, object] = {}
    now = [1_720_000_000.0]

    class FileSystem(LocalFileSystem):
        """仅首次清理删除失败。"""

        def unlink(self, target: Path) -> None:
            """模拟本地权限问题。"""

            raise OSError("cannot remove")

    def run(store: DirectoryObservationStore, filesystem: LocalFileSystem) -> ScanCoordinator:
        """通过新协调器重新取得远端信息。"""

        queue = _Queue()
        coordinator = ScanCoordinator(
            lambda: configuration,
            dispatch=queue,
            client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
            clock=lambda: now[0],
            observation_store=store,
            filesystem=filesystem,
        )
        assert coordinator.trigger(ScanTarget("a", mapping_index=1)).accepted
        queue.run()
        return coordinator

    store = DirectoryObservationStore(
        lambda: persisted.get("value"), lambda value: persisted.__setitem__("value", value)
    )
    assert run(store, FileSystem()).status()["state"] == "failed"
    identity = ("a", "/media/movies", str(output))
    assert stale.exists()
    assert store.directory(identity, "/media/movies").needs_check is False
    assert store.directory(identity, "/media/movies/Season").successful_at == now[0]
    recovered = DirectoryObservationStore(
        lambda: persisted["value"], lambda value: persisted.__setitem__("value", value)
    )
    assert recovered.directory(identity, "/media/movies/Season").cleanup_pending is True
    now[0] += 3600
    assert run(recovered, LocalFileSystem()).status()["state"] == "success"
    assert not stale.exists()
    assert recovered.directory(identity, "/media/movies/Season").successful_at == now[0]
    assert recovered.directory(identity, "/media/movies/Season").needs_check is False
    assert transport.pages("/media/movies/Season") == [1, 1]


def _observation_task(
    tmp_path: Path,
    tree: dict[str, list[dict[str, Any]]],
    *,
    exclude_keywords: str = "",
    filesystem: LocalFileSystem | None = None,
) -> tuple[ScanCoordinator, _Queue, _Transport, DirectoryObservationStore, dict[str, object], list[float]]:
    """复用扫描传输与调度替身，返回可推进时钟及内存持久化边界。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "observations",
                    "name": "观测实例",
                    "scan_url": "https://observations.example.invalid",
                    "api_token": "test-only-placeholder",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(tmp_path),
                            "whitelist_mode": "custom",
                            "extensions": [".mkv", ".srt"],
                            "exclude_keywords": exclude_keywords,
                        }
                    ],
                }
            ]
        }
    )
    persisted: dict[str, object] = {}
    store = DirectoryObservationStore(
        lambda: persisted.get("value"), lambda value: persisted.__setitem__("value", value)
    )
    queue = _Queue()
    transport = _Transport({"https://observations.example.invalid": tree}, null_empty_content=True)
    now = [1_720_000_000.0]
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport, per_page=1, max_retries=0),
        media_suffix_provider=lambda: (".mkv",),
        observation_store=store,
        filesystem=filesystem,
        clock=lambda: now[0],
    )
    return coordinator, queue, transport, store, persisted, now


@pytest.mark.parametrize(
    ("entries", "kind", "modified_at"),
    [
        ([], "empty", None),
        ([{"name": "missing.txt", "is_dir": False}], "invalid", None),
        ([{"name": "bad.txt", "is_dir": False, "modified": "not a time"}], "invalid", None),
        ([{"name": "future.txt", "is_dir": False, "modified": "2099-01-01T00:00:00Z"}], "valid", 4_070_908_800.0),
        ([{"name": "Sample.mkv", "is_dir": False}], "empty", None),
        ([{"name": "Sample", "is_dir": True, "modified": "2099-01-01T00:00:00Z"}], "empty", None),
    ],
)
def test_scan_observations_distinguish_empty_invalid_future_and_excluded(
    tmp_path: Path, entries: list[dict[str, Any]], kind: str, modified_at: float | None
) -> None:
    """异常不可判空，未来保留原值，排除文件和整分支不贡献摘要。"""

    coordinator, queue, transport, store, _persisted, now = _observation_task(
        tmp_path, {"/library": entries}, exclude_keywords="Sample"
    )
    identity = ("observations", "/library", str(tmp_path))
    assert store.directory(identity, "/library").successful_at is None
    assert coordinator.trigger().accepted
    queue.run()
    assert coordinator.status()["state"] == "success"
    observation = store.directory(identity, "/library")
    assert observation.fingerprint is not None
    assert observation.successful_at == now[0]
    assert transport.pages("/library/Sample") == []
    assert not list(tmp_path.rglob("*.strm"))
    # 空层、异常与未来时间均可按期剪枝，且排除分支从不列目录。
    assert coordinator.trigger().accepted
    queue.run()
    assert transport.pages("/library") == [1]
    assert transport.pages("/library/Sample") == []


def test_scan_observations_replace_fingerprint_and_remove_disappeared_branch(tmp_path: Path) -> None:
    """文件删除改变本层摘要；确认消失分支撤销历史观测并清理产物。"""

    tree = {
        "/library": [{"name": "Branch", "is_dir": True}],
        "/library/Branch": [
            {"name": "old.mkv", "is_dir": False, "modified": "2024-01-01T00:00:00Z"},
            {"name": "latest.mkv", "is_dir": False, "modified": "2024-03-01T00:00:00Z"},
        ],
    }
    coordinator, queue, transport, store, _persisted, now = _observation_task(tmp_path, tree)
    identity = ("observations", "/library", str(tmp_path))
    assert coordinator.trigger().accepted
    queue.run()
    previous = store.directory(identity, "/library/Branch").fingerprint
    tree["/library/Branch"].pop()
    now[0] += 30 * 86400
    assert coordinator.trigger().accepted
    queue.run()
    assert store.directory(identity, "/library/Branch").fingerprint != previous
    assert not (tmp_path / "Branch" / "latest.strm").exists()
    tree["/library"] = []
    now[0] += 90 * 86400
    assert coordinator.trigger().accepted
    queue.run()
    assert store.directory(identity, "/library").children == ()
    assert store.directory(identity, "/library/Branch").successful_at is None
    assert not (tmp_path / "Branch" / "old.strm").exists()
    assert transport.pages("/library/Branch") == [1, 2, 1]


def test_failed_directory_keeps_last_observation_and_successful_siblings(tmp_path: Path) -> None:
    """后页失败不提交半层结果；父层和兄弟保留成功事实及待清理要求。"""

    tree = {
        "/library": [{"name": "Good", "is_dir": True}, {"name": "Bad", "is_dir": True}],
        "/library/Good": [],
        "/library/Bad": [{"name": "old.mkv", "is_dir": False, "modified": "2024-01-01T00:00:00Z"}],
    }
    coordinator, queue, transport, store, persisted, now = _observation_task(tmp_path, tree)
    identity = ("observations", "/library", str(tmp_path))
    assert coordinator.trigger().accepted
    queue.run()
    old = store.directory(identity, "/library/Bad")
    tree["/library/Bad"] = [
        {"name": "new.mkv", "is_dir": False, "modified": "2024-03-01T00:00:00Z"},
        {"name": "second.txt", "is_dir": False, "modified": "2024-03-01T00:00:00Z"},
    ]
    original_post = transport.post_json

    def post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        """完整第一轮后，只让第二轮的子目录后页失败。"""

        if payload["path"] == "/library/Bad" and payload["page"] == 2:
            return HttpResponse(403, {"code": 403})
        return original_post(url, payload, headers)

    # 使用非认证失败，以验证同映射其他目录继续完成。
    def failed_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        """以不可重试的目录格式故障模拟后页不完整。"""

        response = post(url, payload, headers)
        return (
            HttpResponse(200, {"code": 200, "data": {"content": [], "total": 2}})
            if response.status_code == 403
            else response
        )

    transport.post_json = failed_post
    now[0] += 90 * 86400
    assert coordinator.trigger().accepted
    queue.run()
    assert coordinator.status()["state"] == "failed"
    assert (tmp_path / "Bad" / "old.strm").exists()
    assert not (tmp_path / "Bad" / "new.strm").exists()
    assert store.directory(identity, "/library/Bad").fingerprint == old.fingerprint
    assert store.directory(identity, "/library/Bad").successful_at == old.successful_at
    assert store.directory(identity, "/library/Good").successful_at == now[0]
    assert store.directory(identity, "/library").successful_at == now[0]
    recovered = DirectoryObservationStore(lambda: persisted["value"], lambda _value: None)
    for path in ("/library", "/library/Good", "/library/Bad"):
        assert recovered.directory(identity, path).needs_check is (path == "/library/Bad")


@pytest.mark.parametrize("failure", ["generation", "download"])
def test_output_failure_does_not_submit_directory_success(tmp_path: Path, failure: str) -> None:
    """本层产物失败不提交成功观测，也不回滚成功子层或进行映射清理。"""

    class FileSystem(LocalFileSystem):
        """只让测试指定的本层产物失败。"""

        def write_text_atomic(self, target: Path, content: str) -> bool:
            """模拟 STRM 创建失败。"""

            if failure == "generation":
                raise OSError("cannot write")
            return super().write_text_atomic(target, content)

        def write_bytes_atomic(self, target: Path, chunks: Any) -> bool:
            """模拟附件原子写入失败。"""

            raise OSError("cannot write")

    tree = {
        "/library": [
            {
                "name": "broken.mkv" if failure == "generation" else "broken.srt",
                "is_dir": False,
                "modified": "2024-01-01T00:00:00Z",
            },
            {"name": "Good", "is_dir": True},
        ],
        "/library/Good": [],
    }
    stale = tmp_path / "stale.strm"
    stale.write_text("keep", encoding="utf-8")
    coordinator, queue, _transport, store, _persisted, now = _observation_task(tmp_path, tree, filesystem=FileSystem())
    identity = ("observations", "/library", str(tmp_path))
    assert coordinator.trigger().accepted
    queue.run()
    assert coordinator.status()["state"] == "failed"
    assert stale.exists()
    assert store.directory(identity, "/library").successful_at is None
    assert store.directory(identity, "/library").successful_at is None
    assert store.directory(identity, "/library").successful_at is None
    assert store.directory(identity, "/library/Good").successful_at == now[0]
    assert store.directory(identity, "/library/Good").needs_check is False


def test_stop_preserves_started_and_unstarted_directory_observations(tmp_path: Path) -> None:
    """停止保留旧成功时间，当前层需要重试，已成功层保留观测，未开始层维持原状态。"""

    tree = {
        "/library": [{"name": "Unstarted", "is_dir": True}, {"name": "Current", "is_dir": True}],
        "/library/Unstarted": [],
        "/library/Current": [],
    }
    coordinator, queue, transport, store, persisted, now = _observation_task(tmp_path, tree)
    identity = ("observations", "/library", str(tmp_path))
    assert coordinator.trigger().accepted
    queue.run()
    unstarted = store.directory(identity, "/library/Unstarted")
    current = store.directory(identity, "/library/Current")
    original_post = transport.post_json

    def stop_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        """本层请求返回时停止，不再开始后续处理或目录。"""

        response = original_post(url, payload, headers)
        if payload["path"] == "/library/Current":
            coordinator.stop()
        return response

    transport.post_json = stop_post
    now[0] += 7 * 86400
    assert coordinator.trigger().accepted
    queue.run()
    assert coordinator.status()["state"] == "stopped"
    recovered = DirectoryObservationStore(lambda: persisted["value"], lambda _value: None)
    assert recovered.directory(identity, "/library/Unstarted") == unstarted
    assert recovered.directory(identity, "/library/Current").successful_at == current.successful_at
    assert recovered.directory(identity, "/library/Current").needs_check is True
    assert recovered.directory(identity, "/library").successful_at == now[0]
    assert recovered.directory(identity, "/library").needs_check is False
    assert transport.pages("/library/Unstarted") == [1]


@pytest.mark.parametrize(
    ("stable_seconds", "interval"),
    [
        (0, 3600),
        (86399, 3600),
        (86400, 86400),
        (7 * 86400 - 1, 86400),
        (7 * 86400, 7 * 86400),
        (30 * 86400 - 1, 7 * 86400),
        (30 * 86400, 30 * 86400),
    ],
)
def test_single_layer_recheck_uses_stability_and_exact_deadlines(
    tmp_path: Path, stable_seconds: int, interval: int
) -> None:
    """只有成功观测跨度提升档位，精确到期前无请求，到期即检查。"""

    tasks = _PruningTasks(tmp_path)
    tasks.run()
    tasks.now[0] += stable_seconds
    assert tasks.coordinator.trigger(force_complete=True).accepted
    tasks.queue.run()
    tasks.transport.calls.clear()
    tasks.now[0] += interval - 1
    tasks.run()
    assert tasks.transport.calls == []
    tasks.now[0] += 1
    tasks.run()
    assert tasks.transport.pages("/library") == [1]


class _PruningTasks:
    """以公开任务入口驱动持久化、配置保存、受控远端和重启。"""

    def __init__(self, output: Path) -> None:
        """建立可连续运行的单层映射，不访问真实网络。"""

        self.configuration: dict[str, Any] = {
            "instances": [
                {
                    "id": "pruning",
                    "name": "剪枝实例",
                    "scan_url": "https://pruning.example.invalid",
                    "playback_url": "https://play-old.example.invalid",
                    "api_token": "test-only-placeholder",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(output),
                            "whitelist_mode": "custom",
                            "extensions": [".mkv", ".srt"],
                        }
                    ],
                }
            ]
        }
        self.tree: dict[str, list[dict[str, Any]]] = {
            "/library": [{"name": "Film.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
        }
        self.transport = _Transport({"https://pruning.example.invalid": self.tree})
        self.persisted: dict[str, object] = {}
        self.now = [1_720_000_000.0]
        self.queue = _Queue()
        self.restart()

    def restart(self, filesystem: LocalFileSystem | None = None) -> None:
        """用共享持久化边界重建协调器；重启本身不启动任务。"""

        store = DirectoryObservationStore(
            lambda: self.persisted.get("value"),
            lambda value: self.persisted.__setitem__("value", deepcopy(value)),
        )
        self.coordinator = ScanCoordinator(
            lambda: build_configuration(self.configuration),
            dispatch=self.queue,
            client_factory=lambda url, token: OpenListClient(url, token, transport=self.transport, per_page=2),
            media_suffix_provider=lambda: (".mkv",),
            clock=lambda: self.now[0],
            filesystem=filesystem,
            observation_store=store,
        )

    def run(self) -> None:
        """显式触发普通任务并执行捕获的宿主回调。"""

        assert self.coordinator.trigger().accepted
        self.queue.run()


def _stabilize_except(tasks: _PruningTasks, active_paths: tuple[str, ...] = ()) -> None:
    """通过两轮真实受控扫描建立七天稳定观测，指定层在第二轮发生可观测变化。"""

    tasks.now[0] += 7 * 86400
    for path in active_paths:
        files = [entry for entry in tasks.tree[path] if not entry.get("is_dir")]
        if files:
            files[0]["modified"] = "2025-01-01T00:00:00Z"
        else:
            tasks.tree[path].append({"name": "stability-note.txt", "is_dir": False})
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"


def _pruning_plugin(tasks: _PruningTasks) -> Alist2Strm:
    """用共享 PluginData 重建插件，保存、重启和扫描均不访问真实网络。"""

    plugin = object.__new__(Alist2Strm)
    plugin._plugin_instance_id = "Alist2Strm-pruning"
    plugin._scan_dispatch = tasks.queue
    plugin._scan_clock = lambda: tasks.now[0]
    plugin._scan_client_factory = lambda url, token: OpenListClient(url, token, transport=tasks.transport)
    plugin._scan_media_suffix_provider = lambda: (".mkv",)
    plugin._schedule_update = lambda _plugin_id: None
    plugin.get_data = lambda key, plugin_id=None: tasks.persisted.get(key)
    plugin.save_data = lambda key, value, plugin_id=None: tasks.persisted.__setitem__(key, deepcopy(value))
    plugin.update_config = lambda _config, plugin_id=None: True
    plugin.init_plugin(tasks.configuration)
    return plugin


@pytest.mark.parametrize("minutes", [1, 15, 60, 1440])
def test_active_interval_controls_first_recheck(tmp_path: Path, minutes: int) -> None:
    """新基线不因文件很旧直接变冷，活跃间隔支持完整配置范围。"""

    tasks = _PruningTasks(tmp_path)
    tasks.configuration["active_recheck_minutes"] = minutes
    tasks.run()
    tasks.now[0] += minutes * 60 - 1
    tasks.transport.calls.clear()
    tasks.restart()
    tasks.run()
    assert tasks.transport.calls == []
    tasks.now[0] += 1
    tasks.run()
    assert tasks.transport.pages("/library") == [1]


def test_active_interval_save_reload_and_task_snapshot(tmp_path: Path) -> None:
    """活跃间隔保存生效，排队快照不被修改，延长不能撤销已提前的期限。"""

    tasks = _PruningTasks(tmp_path)
    plugin = _pruning_plugin(tasks)
    saved: dict[str, Any] = {}
    plugin.update_config = lambda value, plugin_id=None: saved.update(deepcopy(value)) or True
    assert plugin.trigger_scan()["accepted"]
    tasks.queue.run()
    tasks.transport.calls.clear()
    assert plugin.trigger_scan()["accepted"]
    tasks.configuration["active_recheck_minutes"] = 15
    assert plugin.save_configuration(tasks.configuration)["data"]["active_recheck_minutes"] == 15
    tasks.now[0] += 899
    tasks.queue.run()
    assert tasks.transport.calls == []
    plugin.init_plugin(saved)
    assert plugin.trigger_scan()["accepted"]
    tasks.queue.run()
    assert tasks.transport.calls == []
    tasks.configuration["active_recheck_minutes"] = 120
    assert plugin.save_configuration(tasks.configuration)["success"]
    plugin.init_plugin(saved)
    tasks.now[0] += 1
    assert plugin.trigger_scan()["accepted"]
    tasks.queue.run()
    assert tasks.transport.pages("/library") == [1]
    tasks.transport.calls.clear()
    tasks.now[0] += 7200 - 1
    assert plugin.trigger_scan()["accepted"]
    tasks.queue.run()
    assert tasks.transport.calls == []
    tasks.now[0] += 1
    assert plugin.trigger_scan()["accepted"]
    tasks.queue.run()
    assert tasks.transport.pages("/library") == [1]


@pytest.mark.parametrize("scan_between_changes", [True, False])
def test_pruning_reactivated_mapping_identity_requires_full_check_after_restart(
    tmp_path: Path, scan_between_changes: bool
) -> None:
    """源目录 A→B→A 即使未扫描 B，重启后 A 也必须完整检查并清理覆盖产物。"""

    tasks = _PruningTasks(tmp_path)
    plugin = _pruning_plugin(tasks)
    assert plugin.trigger_scan()["accepted"]
    tasks.queue.run()
    tasks.tree["/other-library"] = [{"name": "Other.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
    mapping = tasks.configuration["instances"][0]["mappings"][0]
    mapping["source_dir"] = "/other-library"
    assert plugin.save_configuration(tasks.configuration)["success"]
    if scan_between_changes:
        assert plugin.trigger_scan()["accepted"]
        tasks.queue.run()
    else:
        (tmp_path / "Other.strm").write_text("stale covered output", encoding="utf-8")
    assert (tmp_path / "Other.strm").exists()
    mapping["source_dir"] = "/library"
    assert plugin.save_configuration(tasks.configuration)["success"]
    plugin = _pruning_plugin(tasks)
    tasks.transport.calls.clear()

    assert plugin.trigger_scan()["accepted"]
    tasks.queue.run()

    assert tasks.transport.pages("/library") == [1]
    assert plugin.get_scan_status()["state"] == "success"
    assert (tmp_path / "Film.strm").exists()
    assert not (tmp_path / "Other.strm").exists()
    tasks.transport.calls.clear()
    assert plugin.trigger_scan()["accepted"]
    tasks.queue.run()
    assert tasks.transport.calls == []


@pytest.mark.parametrize("previously_completed", [True, False])
def test_pruning_old_snapshot_cannot_complete_reactivated_identity(tmp_path: Path, previously_completed: bool) -> None:
    """身份过渡的版本隔离旧快照，首次排队和已有冷映射均不能清除新必查要求。"""

    tasks = _PruningTasks(tmp_path)
    plugin = _pruning_plugin(tasks)
    if previously_completed:
        assert plugin.trigger_scan()["accepted"]
        tasks.queue.run()
    assert plugin.trigger_scan(ScanTarget("pruning", mapping_index=1), force_complete=True)["accepted"]
    mapping = tasks.configuration["instances"][0]["mappings"][0]
    mapping["source_dir"] = "/other-library"
    assert plugin.save_configuration(tasks.configuration)["success"]
    mapping["source_dir"] = "/library"
    assert plugin.save_configuration(tasks.configuration)["success"]
    tasks.queue.run()
    assert plugin.get_scan_status()["state"] == "success"
    plugin = _pruning_plugin(tasks)
    tasks.transport.calls.clear()

    assert plugin.trigger_scan()["accepted"]
    tasks.queue.run()

    assert tasks.transport.pages("/library") == [1]
    assert plugin.get_scan_status()["state"] == "success"


def test_pruning_identity_transition_persistence_failure_rejects_configuration_save(tmp_path: Path) -> None:
    """身份退休未能持久化时不写宿主配置、不发布候选快照，恢复后仍可保存。"""

    tasks = _PruningTasks(tmp_path)
    plugin = _pruning_plugin(tasks)
    assert plugin.trigger_scan()["accepted"]
    tasks.queue.run()
    tasks.configuration["instances"][0]["mappings"][0]["source_dir"] = "/other-library"
    writes: list[object] = []
    plugin.update_config = lambda value, plugin_id=None: writes.append(value) or True
    save_data = plugin.save_data
    plugin.save_data = lambda key, value, plugin_id=None: (
        False if key == plugin.OBSERVATIONS_DATA_KEY else save_data(key, value, plugin_id=plugin_id)
    )

    with pytest.raises(plugin.PersistenceError, match="目录映射身份状态保存失败"):
        plugin.save_configuration(tasks.configuration)

    assert writes == []
    assert plugin.public_configuration()["instances"][0]["mappings"][0]["source_dir"] == "/library"
    plugin.save_data = save_data
    assert plugin.save_configuration(tasks.configuration)["success"]
    assert len(writes) == 1


@pytest.mark.parametrize("scan_mode", ["force", "early_configuration"])
@pytest.mark.parametrize("save_failure", ["false", "exception"])
def test_pruning_failed_durable_marker_blocks_io_then_preserves_failed_retry_after_restart(
    tmp_path: Path, scan_mode: str, save_failure: str
) -> None:
    """提前或强制检查必须先保存未完成标记；保存失败不启动 I/O，后续失败重启必查。"""

    tasks = _PruningTasks(tmp_path)
    plugin = _pruning_plugin(tasks)
    assert plugin.trigger_scan()["accepted"]
    tasks.queue.run()
    tasks.now[0] += 1
    orphan = tmp_path / "orphan.strm"
    orphan.write_text("protect incomplete mapping", encoding="utf-8")
    if scan_mode == "early_configuration":
        tasks.configuration["instances"][0]["playback_url"] = "https://play-new.example.invalid"
        assert plugin.save_configuration(tasks.configuration)["success"]
    save_data = plugin.save_data

    def fail_marker(key: str, value: Any, plugin_id: str | None = None) -> object:
        """只拒绝目录未完成标记写入，保留真实 PluginData 的其余写入。"""

        if key == plugin.OBSERVATIONS_DATA_KEY and any(
            mapping["directories"].get("/library", {}).get("needs_check") for mapping in value["mappings"]
        ):
            if save_failure == "exception":
                raise OSError("controlled marker persistence failure")
            return False
        return save_data(key, value, plugin_id=plugin_id)

    plugin.save_data = fail_marker
    del tasks.tree["/library"]  # 若越过持久化边界，受控目录 I/O 就会失败。
    tasks.transport.calls.clear()
    assert plugin.trigger_scan(ScanTarget("pruning", mapping_index=1), force_complete=scan_mode == "force")["accepted"]
    tasks.queue.run()

    assert tasks.transport.calls == []
    assert plugin.get_scan_status()["state"] == "failed"
    assert "未完成标记保存失败" in plugin.get_scan_status()["mappings"][0]["error"]
    assert orphan.exists()
    recovered = DirectoryObservationStore(lambda: tasks.persisted[plugin.OBSERVATIONS_DATA_KEY], lambda _value: None)
    assert recovered.directory(("pruning", "/library", str(tmp_path)), "/library").successful_at == tasks.now[0] - 1

    plugin.save_data = save_data
    assert plugin.trigger_scan()["accepted"]
    tasks.queue.run()
    assert tasks.transport.pages("/library")
    assert plugin.get_scan_status()["state"] == "failed"
    assert orphan.exists()
    tasks.tree["/library"] = [{"name": "Film.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
    tasks.now[0] += 3600
    plugin = _pruning_plugin(tasks)
    tasks.transport.calls.clear()
    assert plugin.trigger_scan()["accepted"]
    tasks.queue.run()
    assert tasks.transport.pages("/library") == [1]
    assert plugin.get_scan_status()["state"] == "success"
    assert not orphan.exists()


def test_pruning_selection_change_does_not_update_existing_strm(tmp_path: Path) -> None:
    """白名单变更使冷映射完整检查，但不把选择失效误当播放地址变化。"""

    tasks = _PruningTasks(tmp_path)
    tasks.run()
    target = tmp_path / "Film.strm"
    target.write_text("handmade content", encoding="utf-8")
    tasks.configuration["instances"][0]["mappings"][0]["extensions"].append(".txt")
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.pages("/library") == [1]
    assert target.read_text(encoding="utf-8") == "handmade content"
    assert tasks.coordinator.status()["counters"]["updated"] == 0  # type: ignore[index]


def test_pruning_empty_mapping_rechecks_at_active_interval_and_cleans_checked_range(tmp_path: Path) -> None:
    """空层期限为一个活跃间隔；未扫描保留、到期完整检查后清理。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"] = []
    tasks.run()
    orphan = tmp_path / "orphan.strm"
    orphan.write_text("keep until checked", encoding="utf-8")
    tasks.now[0] += 3600 - 1
    tasks.restart()
    tasks.transport.calls.clear()
    before = deepcopy(tasks.persisted)
    tasks.run()
    assert tasks.transport.calls == []
    assert tasks.persisted == before
    assert orphan.exists()
    tasks.now[0] += 1
    tasks.run()
    assert tasks.transport.pages("/library") == [1]
    assert not orphan.exists()


def test_pruning_deadline_starts_after_layer_processing_not_task_start(tmp_path: Path) -> None:
    """本层处理耗时不吃掉复查间隔，排队等待也不改变已建立期限。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"][0]["modified"] = datetime.fromtimestamp(tasks.now[0] - 40 * 86400, tz=UTC).isoformat()

    class SlowWrite(LocalFileSystem):
        """在本地写入边界推进受控时钟。"""

        def write_text_atomic(self, target: Path, content: str) -> bool:
            """完成一次原子写入后模拟两天处理耗时。"""

            created = super().write_text_atomic(target, content)
            tasks.now[0] += 2 * 86400
            return created

    tasks.restart(SlowWrite())
    tasks.run()
    successful_at = tasks.now[0]
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.now[0] = successful_at + 3600 - 1
    tasks.run()
    assert tasks.transport.calls == []
    # 任务排队后再到期，执行时应检查而不是重设期限。
    assert tasks.coordinator.trigger().accepted
    tasks.now[0] = successful_at + 3600
    tasks.queue.run()
    assert tasks.transport.pages("/library") == [1]


@pytest.mark.parametrize("change", ["playback", "source", "output", "whitelist", "exclude"])
def test_pruning_relevant_configuration_changes_force_complete_mapping(tmp_path: Path, change: str) -> None:
    """五类相关配置变更在重启后仍使未到期冷映射完整处理。"""

    old_output = tmp_path / "old"
    tasks = _PruningTasks(old_output)
    tasks.run()
    tasks.transport.calls.clear()
    instance = tasks.configuration["instances"][0]
    mapping = instance["mappings"][0]
    if change == "playback":
        instance["playback_url"] = "https://play-new.example.invalid"
    elif change == "source":
        mapping["source_dir"] = "/new-library"
        tasks.tree["/new-library"] = deepcopy(tasks.tree["/library"])
    elif change == "output":
        mapping["output_dir"] = str(tmp_path / "new")
    elif change == "whitelist":
        mapping["extensions"] = [".srt"]
    else:
        mapping["exclude_keywords"] = "Film"
    tasks.restart()
    tasks.run()
    assert tasks.transport.pages(mapping["source_dir"]) == [1]
    old_target = old_output / "Film.strm"
    if change == "playback":
        assert old_target.read_text(encoding="utf-8") == "https://play-new.example.invalid/d/library/Film.mkv"
        assert tasks.coordinator.status()["counters"]["updated"] == 1  # type: ignore[index]
    elif change == "exclude":
        # 关键字排除仍按旧清理规则删除，不享受时间剪枝保护。
        assert not old_target.exists()
    else:
        # 退出受管后缀范围或迁移根目录不会回溯删除已有产物。
        assert old_target.exists()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.calls == []


def test_pruning_unrelated_configuration_and_unstarted_stop_preserve_deadline(tmp_path: Path) -> None:
    """计划、历史和名称配置不失效，未开始的停止任务也不修改已有计划。"""

    tasks = _PruningTasks(tmp_path)
    tasks.run()
    tasks.configuration.update({"schedule_cron": "0 1 * * *", "history_retention_days": 1})
    tasks.configuration["instances"][0]["name"] = "改名"
    tasks.restart()
    tasks.transport.calls.clear()
    before = deepcopy(tasks.persisted)
    assert tasks.coordinator.trigger().accepted
    assert tasks.coordinator.stop().accepted
    tasks.queue.run()
    tasks.run()
    assert tasks.transport.calls == []
    assert tasks.persisted == before


def test_pruning_old_task_snapshot_does_not_clear_new_configuration_requirement(tmp_path: Path) -> None:
    """已排队任务只应用自身快照；成功后新播放配置仍必须完整执行。"""

    tasks = _PruningTasks(tmp_path)
    assert tasks.coordinator.trigger().accepted
    tasks.configuration["instances"][0]["playback_url"] = "https://play-new.example.invalid"
    tasks.queue.run()
    target = tmp_path / "Film.strm"
    assert target.read_text(encoding="utf-8") == "https://play-old.example.invalid/d/library/Film.mkv"
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.pages("/library") == [1]
    assert target.read_text(encoding="utf-8") == "https://play-new.example.invalid/d/library/Film.mkv"


def test_pruning_new_branch_builds_all_layers_then_preserves_independent_deadlines(tmp_path: Path) -> None:
    """到期父层发现新分支并完整建档，之后各层未到期均不请求。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"] = []
    tasks.run()
    tasks.tree["/library"] = [{"name": "Season", "is_dir": True}]
    tasks.tree["/library/Season"] = [{"name": "Episode.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
    tasks.now[0] += 7 * 86400
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.pages("/library") == [1]
    assert tasks.transport.pages("/library/Season") == [1]
    assert (tmp_path / "Season" / "Episode.strm").exists()
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.calls == []


@pytest.mark.parametrize("missing", ["all", "root", "configuration"])
def test_pruning_missing_state_requires_complete_check(tmp_path: Path, missing: str) -> None:
    """丢失全部状态、根层观测或配置依据都不能继续使用旧剪枝资格。"""

    tasks = _PruningTasks(tmp_path)
    tasks.run()
    if missing == "all":
        tasks.persisted.clear()
    else:
        payload = tasks.persisted["value"]
        assert isinstance(payload, dict)
        mapping = payload["mappings"][0]
        if missing == "root":
            mapping["directories"].clear()
        else:
            mapping.pop("configuration")
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.pages("/library") == [1]


@pytest.mark.parametrize("failure", ["listing", "write", "download", "cleanup", "cleanup_listing"])
def test_failed_work_retries_at_active_deadline_after_restart(tmp_path: Path, failure: str) -> None:
    """到期失败等待活跃间隔重试，清理失败重试不重新读取冷目录。"""

    tasks = _PruningTasks(tmp_path)
    tasks.run()
    tasks.now[0] += 90 * 86400
    orphan = tmp_path / "orphan.strm"
    orphan.write_text("keep on failed mapping", encoding="utf-8")
    if failure == "listing":
        del tasks.tree["/library"]
    elif failure == "write":
        (tmp_path / "Film.strm").unlink()
    elif failure == "download":
        tasks.tree["/library"].append({"name": "Film.srt", "is_dir": False, "modified": "2023-01-01T00:00:00Z"})
        tasks.transport.stream_factory = lambda _url, _headers: DownloadResponse(status_code=403, body=())

    class BrokenFileSystem(LocalFileSystem):
        """在本地 I/O 边界注入一种失败，不触及扫描内部方法。"""

        def write_text_atomic(self, target: Path, content: str) -> bool:
            """仅模拟目标创建失败。"""

            if failure == "write":
                raise OSError("controlled creation failure")
            return super().write_text_atomic(target, content)

        def unlink(self, target: Path) -> None:
            """仅模拟清理删除失败。"""

            if failure == "cleanup":
                raise OSError("controlled cleanup failure")
            super().unlink(target)

        def iter_files(self, root: Path) -> list[Path]:
            """仅模拟清理枚举失败。"""

            if failure == "cleanup_listing":
                raise OSError("controlled enumeration failure")
            return list(super().iter_files(root))

    tasks.restart(BrokenFileSystem())
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] in {"failed", "partial_failure"}
    assert orphan.exists()
    # 成功观测必须保留，未完成 owner 只需要清理重试。
    if failure in {"cleanup", "cleanup_listing"}:
        recovered = DirectoryObservationStore(lambda: tasks.persisted["value"], lambda _value: None)
        observation = recovered.directory(("pruning", "/library", str(tmp_path)), "/library")
        assert observation.successful_at == tasks.now[0]
        assert observation.cleanup_pending
        assert not observation.needs_check
    if failure == "listing":
        tasks.tree["/library"] = [{"name": "Film.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
    tasks.transport.stream_factory = None
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.now[0] += 3600
    tasks.run()
    assert tasks.transport.pages("/library") == ([] if failure in {"cleanup", "cleanup_listing"} else [1])
    assert tasks.coordinator.status()["state"] == "success"
    assert not orphan.exists()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.calls == []


@pytest.mark.parametrize("interruption", ["processing_stop", "cleanup_stop", "crash"])
def test_pruning_interrupted_work_remains_required_across_restart(tmp_path: Path, interruption: str) -> None:
    """正在处理或清理时停止以及进程中断，都保留必查并重新取得源信息。"""

    tasks = _PruningTasks(tmp_path)
    tasks.run()
    # 提前配置失效触发处理，不依赖原来的九十天期限。
    tasks.configuration["instances"][0]["playback_url"] = "https://play-new.example.invalid"
    orphan = tmp_path / "orphan.strm"
    orphan.write_text("keep until successful cleanup", encoding="utf-8")

    class InterruptingFileSystem(LocalFileSystem):
        """在原子 I/O 结束后注入停止或非异常捕获的进程退出。"""

        def replace_text_atomic(self, target: Path, content: str) -> None:
            """写入完成后停止或模拟进程退出。"""

            super().replace_text_atomic(target, content)
            if interruption == "processing_stop":
                assert tasks.coordinator.stop().accepted
            elif interruption == "crash":
                raise SystemExit("controlled process interruption")

        def unlink(self, target: Path) -> None:
            """清理一项完成后请求停止。"""

            super().unlink(target)
            if interruption == "cleanup_stop":
                assert tasks.coordinator.stop().accepted

    tasks.restart(InterruptingFileSystem())
    if interruption == "crash":
        with pytest.raises(SystemExit, match="controlled process interruption"):
            tasks.run()
    else:
        tasks.run()
        assert tasks.coordinator.status()["state"] == "stopped"
    if interruption != "cleanup_stop":
        assert orphan.exists()
    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.pages("/library") == [1]
    assert tasks.coordinator.status()["state"] == "success"
    assert not orphan.exists()
    assert (tmp_path / "Film.strm").read_text(encoding="utf-8") == "https://play-new.example.invalid/d/library/Film.mkv"
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.calls == []


class _PartialPlaybackUpdate(LocalFileSystem):
    """在本地边界仅让第二个已有目标的播放地址更新失败。"""

    def replace_text_atomic(self, target: Path, content: str) -> None:
        """保留第二个目标内容，允许第一项更新完成。"""

        if target.name == "Other.strm":
            raise OSError("controlled update failure")
        super().replace_text_atomic(target, content)


def test_pruning_playback_failure_retains_full_update_requirement_after_restart(tmp_path: Path) -> None:
    """新播放地址部分更新后失败，持久化要求使重启到期后重试剩余目标。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"].append({"name": "Other.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"})
    tasks.run()
    tasks.configuration["instances"][0]["playback_url"] = "https://play-new.example.invalid"

    tasks.restart(_PartialPlaybackUpdate())
    tasks.run()
    assert tasks.coordinator.status()["state"] == "partial_failure"
    assert (tmp_path / "Film.strm").read_text(encoding="utf-8") == "https://play-new.example.invalid/d/library/Film.mkv"
    assert (tmp_path / "Other.strm").read_text(
        encoding="utf-8"
    ) == "https://play-old.example.invalid/d/library/Other.mkv"
    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.pages("/library") == [1]
    assert (tmp_path / "Other.strm").read_text(
        encoding="utf-8"
    ) == "https://play-new.example.invalid/d/library/Other.mkv"


def test_pruning_coverage_protects_cold_mapping_and_cleans_checked_mapping(tmp_path: Path) -> None:
    """普通任务按映射覆盖保护未扫描产物，另一已检查映射照常清理。"""

    cold_output = tmp_path / "cold"
    active_output = tmp_path / "active"
    tasks = _PruningTasks(cold_output)
    tasks.configuration["instances"][0]["mappings"].append(
        {
            "source_dir": "/active",
            "output_dir": str(active_output),
            "whitelist_mode": "custom",
            "extensions": [".mkv"],
        }
    )
    tasks.tree["/active"] = [
        {"name": "New.mkv", "is_dir": False, "modified": datetime.fromtimestamp(tasks.now[0], tz=UTC).isoformat()}
    ]
    tasks.run()
    _stabilize_except(tasks, ("/active",))
    cold_orphan = cold_output / "old.strm"
    active_orphan = active_output / "old.strm"
    cold_orphan.write_text("unscanned", encoding="utf-8")
    active_orphan.write_text("checked", encoding="utf-8")
    tasks.transport.calls.clear()
    tasks.now[0] += 3600
    tasks.restart()
    tasks.run()
    assert tasks.transport.pages("/library") == []
    assert tasks.transport.pages("/active") == [1]
    assert cold_orphan.exists()
    assert not active_orphan.exists()
    assert tasks.coordinator.status()["counters"]["deleted"] == 1  # type: ignore[index]


def test_pruning_reverting_failed_playback_change_restores_all_existing_strm(tmp_path: Path) -> None:
    """播放地址部分更新失败后改回旧配置，重启仍修复已经写入的新地址。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"].append({"name": "Other.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"})
    tasks.run()
    tasks.configuration["instances"][0]["playback_url"] = "https://play-new.example.invalid"

    tasks.restart(_PartialPlaybackUpdate())
    tasks.run()
    assert tasks.coordinator.status()["state"] == "partial_failure"
    assert (tmp_path / "Film.strm").read_text(encoding="utf-8") == "https://play-new.example.invalid/d/library/Film.mkv"

    tasks.configuration["instances"][0]["playback_url"] = "https://play-old.example.invalid"
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()

    assert tasks.transport.pages("/library") == [1]
    assert tasks.coordinator.status()["state"] == "success"
    assert (tmp_path / "Film.strm").read_text(encoding="utf-8") == "https://play-old.example.invalid/d/library/Film.mkv"
    assert (tmp_path / "Other.strm").read_text(
        encoding="utf-8"
    ) == "https://play-old.example.invalid/d/library/Other.mkv"
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.calls == []


def test_pruning_playback_change_after_failed_first_scan_updates_created_strm(tmp_path: Path) -> None:
    """首次处理部分创建后失败，再改播放地址仍更新已创建目标并完成缺失目标。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"].append({"name": "Other.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"})

    class PartialCreation(LocalFileSystem):
        """在首次创建的第二项注入失败。"""

        def write_text_atomic(self, target: Path, content: str) -> bool:
            """保留第一项正常创建的结果。"""

            if target.name == "Other.strm":
                raise OSError("controlled creation failure")
            return super().write_text_atomic(target, content)

    tasks.restart(PartialCreation())
    tasks.run()
    assert tasks.coordinator.status()["state"] == "partial_failure"
    assert (tmp_path / "Film.strm").read_text(encoding="utf-8") == "https://play-old.example.invalid/d/library/Film.mkv"
    assert not (tmp_path / "Other.strm").exists()

    tasks.configuration["instances"][0]["playback_url"] = "https://play-new.example.invalid"
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()

    assert tasks.transport.pages("/library") == [1]
    assert tasks.coordinator.status()["state"] == "success"
    assert (tmp_path / "Film.strm").read_text(encoding="utf-8") == "https://play-new.example.invalid/d/library/Film.mkv"
    assert (tmp_path / "Other.strm").read_text(
        encoding="utf-8"
    ) == "https://play-new.example.invalid/d/library/Other.mkv"


def test_pruning_stop_during_incomplete_marker_does_not_start_remote_client(tmp_path: Path) -> None:
    """未完成标记保存时收到停止，随后不开始远端操作，重启重新取得源信息。"""

    tasks = _PruningTasks(tmp_path)
    opened: list[str] = []

    def save_and_stop(value: object) -> None:
        """在持久化 I/O 边界保存标记并请求停止。"""

        tasks.persisted["value"] = deepcopy(value)
        if isinstance(value, dict) and any(
            directory["needs_check"] for mapping in value["mappings"] for directory in mapping["directories"].values()
        ):
            assert tasks.coordinator.stop().accepted

    def open_client(url: str, token: str) -> OpenListClient:
        """记录远端资源是否在停止后被打开。"""

        opened.append(url)
        return OpenListClient(url, token, transport=tasks.transport)

    tasks.coordinator = ScanCoordinator(
        lambda: build_configuration(tasks.configuration),
        dispatch=tasks.queue,
        client_factory=open_client,
        media_suffix_provider=lambda: (".mkv",),
        clock=lambda: tasks.now[0],
        observation_store=DirectoryObservationStore(lambda: tasks.persisted.get("value"), save_and_stop),
    )
    tasks.run()

    assert tasks.coordinator.status()["state"] == "stopped"
    assert opened == []
    assert tasks.transport.calls == []
    tasks.now[0] += 3600
    tasks.restart()
    tasks.run()
    assert tasks.transport.pages("/library") == [1]
    assert tasks.coordinator.status()["state"] == "success"


def test_force_complete_runs_only_selected_saved_mapping_with_normal_output_rules(tmp_path: Path) -> None:
    """强制方式仅处理指定快照；筛选、跳过已有产物和清理边界不变。"""

    configuration = _config(tmp_path)
    selected = configuration.instances[0].mappings[0]
    selected.whitelist_mode = "custom"
    selected.extensions = [".mkv", ".srt"]
    selected.exclude_keywords = ["Sample"]
    output = tmp_path / "a"
    output.mkdir()
    existing = output / "existing.strm"
    existing.write_text("handmade", encoding="utf-8")
    stale = output / "stale.strm"
    stale.write_text("stale", encoding="utf-8")
    untouched = tmp_path / "empty" / "stale.strm"
    untouched.parent.mkdir()
    untouched.write_text("other mapping", encoding="utf-8")
    transport = _Transport(
        {
            "https://scan-a.example.invalid": {
                "/media/movies": [
                    {"name": "existing.mkv", "is_dir": False},
                    {"name": "missing.mkv", "is_dir": False},
                    {"name": "missing.srt", "is_dir": False},
                    {"name": "ignored.txt", "is_dir": False},
                    {"name": "Sample", "is_dir": True},
                    {"name": "Allowed", "is_dir": True},
                ],
                "/media/movies/Allowed": [{"name": "deep.mkv", "is_dir": False}],
            }
        }
    )
    queue = _Queue()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=transport),
        media_suffix_provider=lambda: (".mkv",),
    )
    assert coordinator.trigger(ScanTarget("a", mapping_index=1), force_complete=True).accepted
    assert not coordinator.trigger().accepted
    assert len(queue.jobs) == 1
    # 启动后编辑保存对象不改变本任务的白名单、源和输出快照。
    selected.extensions = []
    selected.source_dir = "/changed"
    queue.run()
    assert coordinator.status()["state"] == "success"
    assert coordinator.status()["total_mappings"] == 1
    assert existing.read_text(encoding="utf-8") == "handmade"
    assert (output / "missing.strm").is_file()
    assert (output / "missing.srt").is_file()
    assert (output / "Allowed" / "deep.strm").is_file()
    assert not (output / "ignored.txt").exists()
    assert not (output / "Sample").exists()
    assert not stale.exists()
    assert untouched.read_text(encoding="utf-8") == "other mapping"
    assert [call["payload"]["path"] for call in transport.calls if "path" in call["payload"]] == [
        "/media/movies",
        "/media/movies/Allowed",
    ]


def test_force_complete_accepts_all_saved_mappings_and_rejects_unknown_target(tmp_path: Path) -> None:
    """强制全量入口允许全部映射，仍拒绝不存在的目标。"""

    configuration = _config(tmp_path)
    queue = _Queue()
    coordinator = ScanCoordinator(lambda: configuration, dispatch=queue)
    assert not coordinator.trigger(ScanTarget("missing", mapping_index=1), force_complete=True).accepted
    configuration.instances[0].mappings.append(
        configuration.instances[0].mappings[0].model_copy(update={"output_dir": str(tmp_path / "duplicate")})
    )
    assert not coordinator.trigger(ScanTarget("a", source_dir="/media/movies"), force_complete=True).accepted
    assert coordinator.trigger(force_complete=True).accepted
    assert len(coordinator.status().mappings) == sum(len(instance.mappings) for instance in configuration.instances)
    assert coordinator.stop().accepted


@pytest.mark.parametrize(
    "failure", ["list", "generation", "download", "stop", "cleanup", "cleanup_listing", "cleanup_stop"]
)
def test_force_complete_unfinished_work_is_rechecked_by_next_normal_scan(tmp_path: Path, failure: str) -> None:
    """强制扫描失败/停止/清理失败后，重启的普通任务仍必查并补回。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "repair",
                    "name": "补回实例",
                    "scan_url": "https://repair.example.invalid",
                    "api_token": "synthetic",
                    "mappings": [
                        {
                            "source_dir": "/library",
                            "output_dir": str(tmp_path),
                            "whitelist_mode": "custom",
                            "extensions": [".mkv", ".srt"],
                        }
                    ],
                }
            ]
        }
    )
    tree = {
        "/library": [
            {"name": "movie.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"},
            {"name": "movie.srt", "is_dir": False, "modified": "2023-01-01T00:00:00Z"},
        ]
    }
    transport = _Transport({"https://repair.example.invalid": tree})
    active_failure = [False]
    persisted: dict[str, object] = {}
    now = [1_720_000_000.0]

    class FileSystem(LocalFileSystem):
        """独立模拟产物及清理的本地 I/O 故障。"""

        def write_text_atomic(self, target: Path, content: str) -> bool:
            """按需阻断 STRM 补回。"""

            if active_failure[0] and failure == "generation":
                raise OSError("cannot write")
            return super().write_text_atomic(target, content)

        def write_bytes_atomic(self, target: Path, chunks: Any) -> bool:
            """按需阻断附件补回。"""

            if active_failure[0] and failure == "download":
                raise OSError("cannot write")
            return super().write_bytes_atomic(target, chunks)

        def unlink(self, target: Path) -> None:
            """按需阻断安全清理。"""

            if active_failure[0] and failure == "cleanup":
                raise OSError("cannot remove")
            super().unlink(target)
            if active_failure[0] and failure == "cleanup_stop":
                assert first.stop().accepted

        def iter_files(self, root: Path) -> list[Path]:
            """按需阻断清理候选枚举。"""

            if active_failure[0] and failure == "cleanup_listing":
                raise OSError("cannot enumerate")
            return list(super().iter_files(root))

    def coordinator() -> tuple[ScanCoordinator, _Queue]:
        """以 PluginData 内存分区重建任务，不依赖旧协调器状态。"""

        queue = _Queue()
        store = DirectoryObservationStore(
            lambda: persisted.get("value"), lambda value: persisted.__setitem__("value", value)
        )
        return ScanCoordinator(
            lambda: configuration,
            dispatch=queue,
            observation_store=store,
            filesystem=FileSystem(),
            clock=lambda: now[0],
            client_factory=lambda url, token: OpenListClient(url, token, transport=transport, max_retries=0),
        ), queue

    first, queue = coordinator()
    target = ScanTarget("repair", mapping_index=1)
    assert first.trigger(target).accepted
    queue.run()
    assert first.status()["state"] == "success"
    (tmp_path / "movie.strm").unlink()
    (tmp_path / "movie.srt").unlink()
    stale = tmp_path / "stale.strm"
    stale.write_text("keep until complete", encoding="utf-8")
    active_failure[0] = True
    original_post = transport.post_json

    def failed_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        """只在强制运行期间阻断源，或在列表返回时请求停止。"""

        if active_failure[0] and failure == "list":
            return HttpResponse(403, {"code": 403})
        response = original_post(url, payload, headers)
        if active_failure[0] and failure == "stop":
            first.stop()
        return response

    transport.post_json = failed_post
    now[0] += 60
    assert first.trigger(target, force_complete=True).accepted
    queue.run()
    expected_state = {"list": "failed", "stop": "stopped", "cleanup_stop": "stopped"}.get(failure, "partial_failure")
    assert first.status()["state"] == expected_state
    assert stale.exists() is (failure != "cleanup_stop")
    active_failure[0] = False
    now[0] += 3600
    restarted, queue = coordinator()
    calls_before = len(transport.calls)
    assert restarted.trigger(target).accepted
    queue.run()
    assert restarted.status()["state"] == "success"
    assert len(transport.calls) > calls_before
    assert (tmp_path / "movie.strm").is_file()
    assert (tmp_path / "movie.srt").is_file()
    assert not stale.exists()


def test_force_complete_bypasses_not_due_time_pruning_and_repairs_missing_products(tmp_path: Path) -> None:
    """普通任务保持剪枝，而指定映射强制运行补回缺失的 STRM 与附件。"""

    coordinator, queue, transport, _store, _persisted, now = _observation_task(
        tmp_path,
        {
            "/library": [
                {"name": "movie.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"},
                {"name": "movie.srt", "is_dir": False, "modified": "2023-01-01T00:00:00Z"},
            ]
        },
    )
    target = ScanTarget("observations", mapping_index=1)
    assert coordinator.trigger(target).accepted
    queue.run()
    assert coordinator.status()["state"] == "success"
    (tmp_path / "movie.strm").unlink()
    (tmp_path / "movie.srt").unlink()
    listed = len(transport.pages("/library"))
    now[0] += 60
    assert coordinator.trigger(target).accepted
    queue.run()
    assert coordinator.status()["state"] == "success"
    assert len(transport.pages("/library")) == listed
    assert not (tmp_path / "movie.strm").exists()
    assert not (tmp_path / "movie.srt").exists()

    assert coordinator.trigger(target, force_complete=True).accepted
    queue.run()
    assert coordinator.status()["state"] == "success"
    assert len(transport.pages("/library")) > listed
    assert (tmp_path / "movie.strm").is_file()
    assert (tmp_path / "movie.srt").is_file()
    assert coordinator.status()["counters"]["generated"] == 1
    assert coordinator.status()["counters"]["downloaded"] == 1


def test_force_complete_success_restarts_normal_recheck_deadline(tmp_path: Path) -> None:
    """提前强制成功以本次检查时间安排普通复查，后续普通运行仍剪枝。"""

    tasks = _PruningTasks(tmp_path)
    tasks.run()
    original_success = tasks.now[0]
    tasks.now[0] += 86400
    tasks.transport.calls.clear()
    target = ScanTarget("pruning", mapping_index=1)
    assert tasks.coordinator.trigger(target, force_complete=True).accepted
    tasks.queue.run()
    assert tasks.transport.pages("/library") == [1]
    assert tasks.coordinator.status()["state"] == "success"

    tasks.restart()
    tasks.transport.calls.clear()
    tasks.now[0] = original_success + 2 * 86400 - 1
    assert tasks.coordinator.trigger(target).accepted
    tasks.queue.run()
    assert tasks.transport.calls == []
    tasks.now[0] += 1
    assert tasks.coordinator.trigger(target).accepted
    tasks.queue.run()
    assert tasks.transport.pages("/library") == [1]


def test_force_complete_stopped_before_execution_preserves_normal_deadline(tmp_path: Path) -> None:
    """排队强制任务在执行前停止不改观测，重启后普通任务按旧期限剪枝。"""

    tasks = _PruningTasks(tmp_path)
    tasks.run()
    before = deepcopy(tasks.persisted)
    tasks.now[0] += 60
    tasks.transport.calls.clear()
    assert tasks.coordinator.trigger(ScanTarget("pruning", mapping_index=1), force_complete=True).accepted
    assert tasks.coordinator.stop().accepted
    tasks.queue.run()
    assert tasks.coordinator.status()["state"] == "stopped"
    assert tasks.transport.calls == []
    assert tasks.persisted == before
    tasks.restart()
    tasks.run()
    assert tasks.transport.calls == []
    assert tasks.persisted == before


def test_pruning_parent_and_child_recheck_and_cleanup_independently(tmp_path: Path) -> None:
    """父到期只清本层，随后子到期直接访问子层并保护未检查父层。"""

    tasks = _PruningTasks(tmp_path)
    start = tasks.now[0]
    modified = datetime.fromtimestamp(start - 40 * 86400, tz=UTC).isoformat()
    tasks.tree["/library"].append({"name": "Season", "is_dir": True})
    tasks.tree["/library/Season"] = [{"name": "Episode.mkv", "is_dir": False, "modified": modified}]
    original_post = tasks.transport.post_json

    def slow_child(url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        """首次子层完成比父层晚一分钟，使两个截止期可独立观察。"""

        response = original_post(url, payload, headers)
        if payload["path"] == "/library/Season":
            tasks.now[0] += 60
        return response

    tasks.transport.post_json = slow_child
    tasks.run()
    tasks.transport.post_json = original_post
    root_stale = tmp_path / "root-stale.strm"
    child_stale = tmp_path / "Season" / "child-stale.strm"
    root_stale.write_text("root", encoding="utf-8")
    child_stale.write_text("child", encoding="utf-8")
    tasks.now[0] = start + 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library") == [1]
    assert tasks.transport.pages("/library/Season") == []
    assert not root_stale.exists()
    assert child_stale.exists()

    root_stale.write_text("unobserved root", encoding="utf-8")
    tasks.now[0] = start + 3660 - 1
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.calls == []
    tasks.now[0] += 1
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library") == []
    assert tasks.transport.pages("/library/Season") == [1]
    assert root_stale.exists()
    assert not child_stale.exists()
    assert (tmp_path / "Season" / "Episode.strm").exists()


def test_pruning_skipped_directory_alias_protects_resolved_products(tmp_path: Path) -> None:
    """父层已查时，未查子层的内部目录别名仍保护实际产物。"""

    tasks = _PruningTasks(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    tasks.tree["/library"][0]["modified"] = datetime.fromtimestamp(tasks.now[0], tz=UTC).isoformat()
    tasks.tree["/library"].append({"name": "alias", "is_dir": True})
    tasks.tree["/library/alias"] = [{"name": "Movie.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
    tasks.run()
    _stabilize_except(tasks, ("/library",))
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.now[0] += 3600
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library") == [1]
    assert tasks.transport.pages("/library/alias") == []
    assert (real / "Movie.strm").is_file()


@pytest.mark.parametrize("modified", [None, "2023-01-01T00:00:00Z", "2099-01-01T00:00:00Z"])
def test_descendant_changes_do_not_heat_ancestors(tmp_path: Path, modified: str | None) -> None:
    """后代频繁变化不提前祖先期限，包括缺失或未来文件时间。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"].append({"name": "Season", "is_dir": True})
    tasks.tree["/library/Season"] = []
    tasks.run()
    _stabilize_except(tasks, ("/library/Season",))
    store = DirectoryObservationStore(lambda: tasks.persisted["value"], lambda _value: None)
    identity = ("pruning", "/library", str(tmp_path))
    parent = store.directory(identity, "/library")
    for number in range(3):
        tasks.tree["/library/Season"].append({"name": f"note{number}.txt", "is_dir": False, "modified": modified})
        tasks.now[0] += 3600
        tasks.restart()
        tasks.transport.calls.clear()
        tasks.run()
        assert tasks.transport.pages("/library") == []
        assert tasks.transport.pages("/library/Season")
        recovered = DirectoryObservationStore(lambda: tasks.persisted["value"], lambda _value: None)
        assert recovered.directory(identity, "/library").successful_at == parent.successful_at
        assert recovered.directory(identity, "/library").recheck_at == parent.recheck_at


def test_pruning_cooling_descendant_does_not_postpone_ancestor_deadline(tmp_path: Path) -> None:
    """父层成功时仍汇总未查后代，后代最大值下降不能后移父层已定期限。"""

    tasks = _PruningTasks(tmp_path)
    start = tasks.now[0]
    tasks.tree["/library"].append({"name": "Season", "is_dir": True})
    tasks.tree["/library/Season"] = [
        {"name": "old.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"},
        {
            "name": "latest.mkv",
            "is_dir": False,
            "modified": datetime.fromtimestamp(start - 40 * 86400, tz=UTC).isoformat(),
        },
    ]
    tasks.run()
    tasks.tree["/library/Season"].pop()
    tasks.tree["/library/Season"].append({"name": "old.srt", "is_dir": False, "modified": "2023-01-01T00:00:00Z"})
    tasks.now[0] = start + 7 * 86400
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.pages("/library") == [1]
    assert tasks.transport.pages("/library/Season") == [1]
    assert not (tmp_path / "Season" / "latest.strm").exists()
    assert (tmp_path / "Season" / "old.srt").is_file()
    tasks.now[0] = start + 14 * 86400 - 1
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.pages("/library") == []
    assert tasks.transport.pages("/library/Season") == [1]
    tasks.transport.calls.clear()
    tasks.now[0] += 1
    tasks.run()
    assert tasks.transport.pages("/library") == [1]
    assert tasks.transport.pages("/library/Season") == []


@pytest.mark.parametrize("change", ["removed", "renamed"])
def test_pruning_due_ancestor_confirms_branch_changes_before_accessing_due_child(tmp_path: Path, change: str) -> None:
    """祖先优先撤销消失/改名分支状态，不请求旧分支，也不清理仍存在兄弟。"""

    tasks = _PruningTasks(tmp_path)
    start = tasks.now[0]
    tasks.tree["/library"] = [{"name": "Gone", "is_dir": True}, {"name": "Sibling", "is_dir": True}]
    modified = datetime.fromtimestamp(start - 40 * 86400, tz=UTC).isoformat()
    tasks.tree["/library/Gone"] = [{"name": "Film.mkv", "is_dir": False, "modified": modified}]
    tasks.tree["/library/Sibling"] = [{"name": "Kept.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
    tasks.run()
    _stabilize_except(tasks, ("/library", "/library/Gone"))
    tasks.tree["/library"] = [{"name": "Sibling", "is_dir": True}]
    del tasks.tree["/library/Gone"]
    if change == "renamed":
        tasks.tree["/library"].append({"name": "Renamed", "is_dir": True})
        tasks.tree["/library/Renamed"] = [{"name": "Film.mkv", "is_dir": False, "modified": modified}]
    sibling_orphan = tmp_path / "Sibling" / "unobserved.strm"
    sibling_orphan.write_text("protect skipped sibling", encoding="utf-8")
    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library") == [1]
    assert tasks.transport.pages("/library/Gone") == []
    assert tasks.transport.pages("/library/Sibling") == []
    assert not (tmp_path / "Gone" / "Film.strm").exists()
    assert (tmp_path / "Sibling" / "Kept.strm").exists()
    assert sibling_orphan.exists()
    assert tasks.transport.pages("/library/Renamed") == ([1] if change == "renamed" else [])
    # 父层保留旧历史的七天期限；消失旧分支记录不能在未来任务复活。
    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.pages("/library/Gone") == []
    assert tasks.transport.pages("/library/Sibling") == []
    assert sibling_orphan.exists()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_pruning_local_state_damage_rebuilds_only_affected_branch(tmp_path: Path, damage: str) -> None:
    """局部坏层完整重建允许的后代，仍有效的兄弟分支继续独立剪枝。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"] = [{"name": "Broken", "is_dir": True}, {"name": "Valid", "is_dir": True}]
    tasks.tree["/library/Broken"] = [{"name": "Deep", "is_dir": True}]
    tasks.tree["/library/Broken/Deep"] = [{"name": "Film.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
    tasks.tree["/library/Valid"] = [{"name": "Kept.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
    tasks.run()
    payload = tasks.persisted["value"]
    assert isinstance(payload, dict)
    directories = payload["mappings"][0]["directories"]
    if damage == "missing":
        del directories["/library/Broken"]
    else:
        directories["/library/Broken"]["children"] = ["/library/Valid"]
    rebuilt = tmp_path / "Broken" / "Deep" / "Film.strm"
    rebuilt.unlink()
    skipped = tmp_path / "Valid" / "Kept.strm"
    skipped.unlink()
    protected = tmp_path / "Valid" / "orphan.strm"
    protected.write_text("unobserved", encoding="utf-8")
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.now[0] += 1
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library") == []
    assert tasks.transport.pages("/library/Broken") == [1]
    assert tasks.transport.pages("/library/Broken/Deep") == [1]
    assert tasks.transport.pages("/library/Valid") == []
    assert rebuilt.exists()
    assert not skipped.exists()
    assert protected.exists()
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.calls == []


@pytest.mark.parametrize("modified", [None, "broken", "2099-01-01T00:00:00Z"])
def test_pruning_invalid_deep_observation_allows_independent_cooling(tmp_path: Path, modified: str | None) -> None:
    """无效或未来的深层文件时间不阻止本层及祖先独立冷却。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"] = [{"name": "Branch", "is_dir": True, "modified": "2023-01-01T00:00:00Z"}]
    tasks.tree["/library/Branch"] = [{"name": "Deep", "is_dir": True}]
    tasks.tree["/library/Branch/Deep"] = [{"name": "note.txt", "is_dir": False, "modified": modified}]
    tasks.run()
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.now[0] += 1
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.calls == []
    tasks.now[0] += 86400
    tasks.run()
    tasks.transport.calls.clear()
    tasks.now[0] += 3600
    tasks.run()
    assert tasks.transport.calls == []


def test_pruning_three_levels_revisit_deep_empty_directory_under_skipped_ancestors(tmp_path: Path) -> None:
    """深层空目录独立到期时不依赖两个冷祖先，也不清理未查范围。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"].append({"name": "Branch", "is_dir": True})
    tasks.tree["/library/Branch"] = [
        {"name": "Old.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"},
        {"name": "Empty", "is_dir": True},
    ]
    tasks.tree["/library/Branch/Empty"] = []
    tasks.run()
    _stabilize_except(tasks, ("/library/Branch/Empty",))
    orphan = tmp_path / "Branch" / "Empty" / "orphan.strm"
    orphan.parent.mkdir()
    orphan.write_text("clean checked layer", encoding="utf-8")
    protected = tmp_path / "Branch" / "orphan.strm"
    protected.write_text("preserve unobserved layer", encoding="utf-8")
    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert [call["payload"]["path"] for call in tasks.transport.calls] == ["/library/Branch/Empty"]
    assert not orphan.exists()
    assert protected.exists()


@pytest.mark.parametrize("failure", ["listing", "write", "download", "stop"])
def test_pruning_partial_multilayer_failure_preserves_mapping_and_rechecks_started_layers(
    tmp_path: Path, failure: str
) -> None:
    """子层失败或停止保护整映射，恢复后成功层保留观测并重试对应清理。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"].extend([{"name": "Bad", "is_dir": True}, {"name": "Good", "is_dir": True}])
    tasks.tree["/library/Bad"] = []
    tasks.tree["/library/Good"] = []
    tasks.run()
    _stabilize_except(tasks, ("/library/Bad", "/library/Good"))
    root_orphan = tmp_path / "orphan.strm"
    root_orphan.write_text("unobserved root", encoding="utf-8")
    good_orphan = tmp_path / "Good" / "orphan.strm"
    good_orphan.parent.mkdir()
    good_orphan.write_text("mapping failure protects completed sibling", encoding="utf-8")
    tasks.tree["/library/Bad"] = [
        {
            "name": "new.srt" if failure == "download" else "new.mkv",
            "is_dir": False,
            "modified": "2023-01-01T00:00:00Z",
        }
    ]
    original_post = tasks.transport.post_json
    original_stream = tasks.transport.stream_factory

    def failing_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        """直接访问子层失败不能被解释为祖先确认删除。"""

        response = original_post(url, payload, headers)
        if payload["path"] == "/library/Bad":
            if failure == "listing":
                return HttpResponse(200, {"code": 200, "data": {"content": [], "total": 1}})
            if failure == "stop":
                tasks.coordinator.stop()
        return response

    class BrokenWrite(LocalFileSystem):
        """仅在失败任务的生成边界阻断子层产物。"""

        def write_text_atomic(self, target: Path, content: str) -> bool:
            """模拟目录内文件处理失败。"""

            if failure == "write":
                raise OSError("cannot write")
            return super().write_text_atomic(target, content)

    tasks.transport.post_json = failing_post
    if failure == "download":
        tasks.transport.stream_factory = lambda _url, _headers: DownloadResponse(status_code=403, body=())
    tasks.now[0] += 3600
    tasks.restart(BrokenWrite())
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == ("stopped" if failure == "stop" else "failed")
    assert tasks.transport.pages("/library") == []
    assert tasks.transport.pages("/library/Good") == [1]
    assert tasks.transport.pages("/library/Bad") == [1]
    assert root_orphan.exists()
    assert good_orphan.exists()
    tasks.transport.post_json = original_post
    tasks.transport.stream_factory = original_stream
    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library") == []
    assert tasks.transport.pages("/library/Good") == [1]
    assert tasks.transport.pages("/library/Bad") == [1]
    assert root_orphan.exists()
    assert not good_orphan.exists()
    assert (tmp_path / "Bad" / ("new.srt" if failure == "download" else "new.strm")).exists()


@pytest.mark.parametrize("failure", ["unlink", "stop", "enumerate"])
def test_pruning_removed_branch_cleanup_retry_remains_with_parent(tmp_path: Path, failure: str) -> None:
    """已移除分支状态后，清理失败或停止仍由父层保存的清理范围负责重试。"""

    tasks = _PruningTasks(tmp_path)
    start = tasks.now[0]
    tasks.tree["/library"].extend([{"name": "Gone", "is_dir": True}, {"name": "Sibling", "is_dir": True}])
    modified = datetime.fromtimestamp(start - 40 * 86400, tz=UTC).isoformat()
    tasks.tree["/library/Gone"] = [
        {"name": "First.mkv", "is_dir": False, "modified": modified},
        {"name": "Second.mkv", "is_dir": False, "modified": modified},
    ]
    tasks.tree["/library/Sibling"] = [{"name": "Kept.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
    tasks.run()
    _stabilize_except(tasks, ("/library", "/library/Gone"))
    protected = tmp_path / "Sibling" / "orphan.strm"
    protected.write_text("skipped sibling", encoding="utf-8")
    tasks.tree["/library"] = [entry for entry in tasks.tree["/library"] if entry["name"] != "Gone"]
    del tasks.tree["/library/Gone"]

    class BrokenCleanup(LocalFileSystem):
        """在清理边界模拟三种未完成方式。"""

        def unlink(self, target: Path) -> None:
            """删除失败或删除原子操作完成后停止。"""

            if failure == "unlink":
                raise OSError("cannot remove")
            super().unlink(target)
            if failure == "stop":
                tasks.coordinator.stop()

        def iter_files(self, root: Path) -> list[Path]:
            """枚举失败时已成功层保留清理要求。"""

            if failure == "enumerate":
                raise OSError("cannot enumerate")
            return list(super().iter_files(root))

    tasks.now[0] += 3600
    tasks.restart(BrokenCleanup())
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == ("stopped" if failure == "stop" else "partial_failure")
    assert tasks.transport.pages("/library") == [1]
    assert tasks.transport.pages("/library/Gone") == []
    assert tasks.transport.pages("/library/Sibling") == []
    assert list((tmp_path / "Gone").glob("*.strm"))
    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library") == [1]
    assert tasks.transport.pages("/library/Gone") == []
    assert tasks.transport.pages("/library/Sibling") == []
    assert not list((tmp_path / "Gone").glob("*.strm"))
    assert protected.exists()
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.calls == []


@pytest.mark.parametrize("seconds_until_due", range(1, 7))
def test_pruning_deadline_crossed_during_selection_does_not_fail_task(tmp_path: Path, seconds_until_due: int) -> None:
    """资格判断期间时钟自然越过期限，也能打开客户端并正常复查。"""

    tasks = _PruningTasks(tmp_path)
    tasks.run()
    tasks.now[0] += 3600 - seconds_until_due
    tasks.transport.calls.clear()

    def ticking_clock() -> float:
        """每个任务时钟边界推进一秒，无实际等待。"""

        tasks.now[0] += 1
        return tasks.now[0]

    tasks.coordinator = ScanCoordinator(
        lambda: build_configuration(tasks.configuration),
        dispatch=tasks.queue,
        client_factory=lambda url, token: OpenListClient(url, token, transport=tasks.transport),
        media_suffix_provider=lambda: (".mkv",),
        clock=ticking_clock,
        observation_store=DirectoryObservationStore(
            lambda: tasks.persisted.get("value"), lambda value: tasks.persisted.__setitem__("value", deepcopy(value))
        ),
    )
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.coordinator.status()["counters"]["failed"] == 0
    assert (tmp_path / "Film.strm").exists()


def test_pruning_failed_ancestor_listing_does_not_hide_known_due_child(tmp_path: Path) -> None:
    """祖先读取失败不能证明分支消失；已知到期子层仍处理但整映射禁清理。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"].extend([{"name": "Due", "is_dir": True}, {"name": "Cold", "is_dir": True}])
    modified = datetime.fromtimestamp(tasks.now[0] - 40 * 86400, tz=UTC).isoformat()
    tasks.tree["/library/Due"] = [{"name": "Film.mkv", "is_dir": False, "modified": modified}]
    tasks.tree["/library/Cold"] = [{"name": "Kept.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
    tasks.run()
    _stabilize_except(tasks, ("/library", "/library/Due"))
    product = tmp_path / "Due" / "Film.strm"
    product.unlink()
    orphan = tmp_path / "Due" / "orphan.strm"
    orphan.write_text("keep on ancestor failure", encoding="utf-8")
    original_post = tasks.transport.post_json

    def failed_root(url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        """仅让父层返回分页不完整，保留可单独访问的子层。"""

        if payload["path"] == "/library":
            return HttpResponse(200, {"code": 200, "data": {"content": [], "total": 1}})
        return original_post(url, payload, headers)

    tasks.transport.post_json = failed_root
    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "partial_failure"
    assert tasks.transport.pages("/library/Due") == [1]
    assert tasks.transport.pages("/library/Cold") == []
    assert product.exists()
    assert orphan.exists()
    tasks.transport.post_json = original_post
    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library/Due") == [1]
    assert tasks.transport.pages("/library/Cold") == []
    assert not orphan.exists()


def test_pruning_checked_directory_alias_cleans_its_scope_below_skipped_parent(tmp_path: Path) -> None:
    """通过目录别名检查子层后，祖先保护不能吞掉子层实际覆盖范围。"""

    tasks = _PruningTasks(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    tasks.tree["/library"].append({"name": "alias", "is_dir": True})
    tasks.tree["/library/alias"] = []
    tasks.run()
    _stabilize_except(tasks, ("/library/alias",))
    stale = real / "orphan.strm"
    stale.write_text("covered by alias", encoding="utf-8")
    root_stale = tmp_path / "orphan.strm"
    root_stale.write_text("unchecked root", encoding="utf-8")
    tasks.tree["/library/alias"] = [{"name": "Film.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library") == []
    assert tasks.transport.pages("/library/alias") == [1]
    assert not stale.exists()
    assert root_stale.exists()
    assert (real / "Film.strm").exists()


def test_pruning_rebuilt_layer_discards_disappeared_orphan_state_before_branch_returns(tmp_path: Path) -> None:
    """中间层记录损坏后确认消失的后代，重现时必须当作新分支建立记录。"""

    tasks = _PruningTasks(tmp_path)
    old_file = {"name": "Film.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}
    tasks.tree["/library"].append({"name": "Branch", "is_dir": True})
    tasks.tree["/library/Branch"] = [{"name": "Deep", "is_dir": True}]
    tasks.tree["/library/Branch/Deep"] = [old_file]
    tasks.run()
    payload = tasks.persisted["value"]
    assert isinstance(payload, dict)
    del payload["mappings"][0]["directories"]["/library/Branch"]
    tasks.tree["/library/Branch"] = [
        {"name": "Recent.mkv", "is_dir": False, "modified": datetime.fromtimestamp(tasks.now[0], tz=UTC).isoformat()}
    ]
    tasks.restart()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert not (tmp_path / "Branch" / "Deep" / "Film.strm").exists()
    tasks.tree["/library/Branch"].append({"name": "Deep", "is_dir": True})
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.now[0] += 3600
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library/Branch/Deep") == [1]
    assert (tmp_path / "Branch" / "Deep" / "Film.strm").exists()


def test_force_complete_cleanup_failure_rechecks_only_failed_owner_after_restart(tmp_path: Path) -> None:
    """强制任务清理失败只保留受影响层清理要求，远端观测保持各自复查计划。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"].extend([{"name": "Broken", "is_dir": True}, {"name": "Valid", "is_dir": True}])
    for branch in ("Broken", "Valid"):
        tasks.tree[f"/library/{branch}"] = [{"name": "Film.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
    tasks.run()
    _stabilize_except(tasks)
    broken_orphan = tmp_path / "Broken" / "orphan.strm"
    valid_orphan = tmp_path / "Valid" / "orphan.strm"
    broken_orphan.write_text("retry failed owner", encoding="utf-8")
    valid_orphan.write_text("clean successful owner", encoding="utf-8")

    class BrokenCleanup(LocalFileSystem):
        """只让一个目录的过期产物删除失败。"""

        def unlink(self, target: Path) -> None:
            """保留失败项，允许其他已查层完成清理。"""

            if target == broken_orphan:
                raise OSError("controlled owner cleanup failure")
            super().unlink(target)

    tasks.now[0] += 1
    tasks.restart(BrokenCleanup())
    assert tasks.coordinator.trigger(ScanTarget("pruning", mapping_index=1), force_complete=True).accepted
    tasks.queue.run()
    assert tasks.coordinator.status()["state"] == "partial_failure"
    assert broken_orphan.exists()
    assert not valid_orphan.exists()

    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library") == []
    assert tasks.transport.pages("/library/Broken") == []
    assert tasks.transport.pages("/library/Valid") == []
    assert not broken_orphan.exists()
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.calls == []


@pytest.mark.parametrize("interruption", ["processing_stop", "crash", "permission"])
def test_force_complete_interrupted_multilayer_task_preserves_unstarted_plan(tmp_path: Path, interruption: str) -> None:
    """强制任务中断保护整映射，失败层重试，成功层及未开始层各自保留复查计划。"""

    tasks = _PruningTasks(tmp_path)
    start = tasks.now[0]
    tasks.tree["/library"].extend([{"name": branch, "is_dir": True} for branch in ("Pending", "Broken", "Done")])
    for branch in ("Pending", "Broken", "Done"):
        tasks.tree[f"/library/{branch}"] = [{"name": "Film.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"}]
    tasks.run()
    _stabilize_except(tasks)
    start = tasks.now[0]
    orphans = [
        tmp_path / "orphan.strm",
        *(tmp_path / branch / "orphan.strm" for branch in ("Pending", "Broken", "Done")),
    ]
    for orphan in orphans:
        orphan.write_text("mapping interruption protects outputs", encoding="utf-8")
    tasks.tree["/library/Broken"].append({"name": "New.mkv", "is_dir": False, "modified": "2023-01-01T00:00:00Z"})
    original_post = tasks.transport.post_json

    def failing_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        """在目录读取边界模拟权限失败，禁止访问剩余子层。"""

        response = original_post(url, payload, headers)
        if interruption == "permission" and payload["path"] == "/library/Broken":
            return HttpResponse(403, {"code": 403, "data": None})
        return response

    class InterruptingWrite(LocalFileSystem):
        """仅在强制新增产物的原子操作后停止或模拟进程退出。"""

        def write_text_atomic(self, target: Path, content: str) -> bool:
            """已经写入的产物不能使尚未清理的层提前解除必查。"""

            created = super().write_text_atomic(target, content)
            if target.name == "New.strm":
                if interruption == "processing_stop":
                    assert tasks.coordinator.stop().accepted
                elif interruption == "crash":
                    raise SystemExit("controlled forced interruption")
            return created

    tasks.transport.post_json = failing_post
    tasks.now[0] += 1
    tasks.restart(InterruptingWrite())
    tasks.transport.calls.clear()
    assert tasks.coordinator.trigger(ScanTarget("pruning", mapping_index=1), force_complete=True).accepted
    if interruption == "crash":
        with pytest.raises(SystemExit, match="controlled forced interruption"):
            tasks.queue.run()
    else:
        tasks.queue.run()
        assert tasks.coordinator.status()["state"] == (
            "stopped" if interruption == "processing_stop" else "partial_failure"
        )
    assert tasks.transport.pages("/library") == [1, 2]
    assert tasks.transport.pages("/library/Done") == [1]
    assert tasks.transport.pages("/library/Broken") == [1]
    assert tasks.transport.pages("/library/Pending") == []
    assert all(orphan.exists() for orphan in orphans)

    tasks.transport.post_json = original_post
    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library") == []
    assert tasks.transport.pages("/library/Done") == []
    assert tasks.transport.pages("/library/Broken") == [1]
    assert tasks.transport.pages("/library/Pending") == []
    assert (tmp_path / "Broken" / "New.strm").exists()
    assert orphans[1].exists()
    assert all(not orphan.exists() for orphan in (orphans[0], orphans[2], orphans[3]))

    tasks.now[0] = start + 7 * 86400 - 1
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.pages("/library/Pending") == []
    assert tasks.transport.pages("/library") == []
    tasks.now[0] += 1
    tasks.transport.calls.clear()
    tasks.run()
    assert [call["payload"]["path"] for call in tasks.transport.calls] == ["/library/Pending"]
    assert not orphans[1].exists()


def test_pruning_integrated_paged_request_cost_for_first_normal_due_and_force(tmp_path: Path) -> None:
    """同一分页树的首次、未到期、局部到期及强制任务只请求应查层。"""

    tasks = _PruningTasks(tmp_path)
    start = tasks.now[0]
    old = "2023-01-01T00:00:00Z"
    warm = datetime.fromtimestamp(start - 40 * 86400, tz=UTC).isoformat()
    tasks.tree["/library"].extend([{"name": "Cold", "is_dir": True}, {"name": "Warm", "is_dir": True}])
    tasks.tree["/library/Cold"] = [
        {"name": "First.mkv", "is_dir": False, "modified": old},
        {"name": "Second.mkv", "is_dir": False, "modified": old},
        {"name": "Deep", "is_dir": True},
    ]
    tasks.tree["/library/Cold/Deep"] = [
        {"name": f"Film{index}.mkv", "is_dir": False, "modified": old} for index in range(5)
    ]
    tasks.tree["/library/Warm"] = [
        {"name": f"Film{index}.mkv", "is_dir": False, "modified": warm} for index in range(3)
    ]
    full_pages = {
        "/library": [1, 2],
        "/library/Warm": [1, 2],
        "/library/Cold": [1, 2],
        "/library/Cold/Deep": [1, 2, 3],
    }
    request_counts: list[int] = []

    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.coordinator.status()["counters"]["generated"] == 11  # type: ignore[index]
    assert {path: tasks.transport.pages(path) for path in full_pages} == full_pages
    request_counts.append(len(tasks.transport.calls))
    original_outputs = {path: path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.strm")}
    assert len(original_outputs) == 11

    tasks.now[0] += 1
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    request_counts.append(len(tasks.transport.calls))
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.calls == []

    _stabilize_except(tasks, ("/library", "/library/Warm"))
    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert {path: tasks.transport.pages(path) for path in full_pages} == {
        "/library": [1, 2],
        "/library/Warm": [1, 2],
        "/library/Cold": [],
        "/library/Cold/Deep": [],
    }
    request_counts.append(len(tasks.transport.calls))

    tasks.now[0] += 1
    tasks.restart()
    tasks.transport.calls.clear()
    assert tasks.coordinator.trigger(ScanTarget("pruning", mapping_index=1), force_complete=True).accepted
    tasks.queue.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.coordinator.status()["counters"]["skipped"] == 11  # type: ignore[index]
    assert {path: tasks.transport.pages(path) for path in full_pages} == full_pages
    request_counts.append(len(tasks.transport.calls))
    assert request_counts == [9, 0, 4, 9]
    assert {path: path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.strm")} == original_outputs


def test_pruning_integrated_branch_changes_force_snapshot_stop_and_recovery(tmp_path: Path) -> None:
    """连续任务覆盖深层到期、补旧附件、删改名、强制快照及配置更新停止重启。"""

    tasks = _PruningTasks(tmp_path)
    start = tasks.now[0]
    old = "2023-01-01T00:00:00Z"
    warm = datetime.fromtimestamp(start - 40 * 86400, tz=UTC).isoformat()
    tasks.tree["/library"].extend([{"name": "Cold", "is_dir": True}, {"name": "Warm", "is_dir": True}])
    tasks.tree["/library/Cold"] = [
        {"name": "Keep.mkv", "is_dir": False, "modified": old},
        {"name": "Delete.mkv", "is_dir": False, "modified": old},
        {"name": "Deep", "is_dir": True},
    ]
    tasks.tree["/library/Cold/Deep"] = []
    tasks.tree["/library/Warm"] = [{"name": "Warm.mkv", "is_dir": False, "modified": warm}]
    tasks.run()
    _stabilize_except(tasks, ("/library", "/library/Warm", "/library/Cold/Deep"))
    start = tasks.now[0]
    tasks.tree["/library/Cold"].append({"name": "Old.srt", "is_dir": False, "modified": old})
    tasks.tree["/library/Cold/Deep"] = [{"name": "Deep.mkv", "is_dir": False, "modified": old}]
    cold_orphan = tmp_path / "Cold" / "orphan.strm"
    deep_orphan = tmp_path / "Cold" / "Deep" / "orphan.strm"
    deep_orphan.parent.mkdir()
    cold_orphan.write_text("unobserved parent", encoding="utf-8")
    deep_orphan.write_text("checked descendant", encoding="utf-8")
    tasks.now[0] += 1
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.calls == []
    assert not (tmp_path / "Cold" / "Old.srt").exists()
    assert not (tmp_path / "Cold" / "Deep" / "Deep.strm").exists()

    tasks.now[0] = start + 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library") == [1, 2]
    assert tasks.transport.pages("/library/Warm") == [1]
    assert tasks.transport.pages("/library/Cold") == []
    assert tasks.transport.pages("/library/Cold/Deep") == [1]
    assert (tmp_path / "Cold" / "Deep" / "Deep.strm").exists()
    assert cold_orphan.exists()
    assert not deep_orphan.exists()
    assert not (tmp_path / "Cold" / "Old.srt").exists()

    tasks.now[0] += 1
    tasks.transport.calls.clear()
    assert tasks.coordinator.trigger(ScanTarget("pruning", mapping_index=1), force_complete=True).accepted
    tasks.queue.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert tasks.transport.pages("/library/Cold") == [1, 2]
    assert (tmp_path / "Cold" / "Old.srt").read_bytes() == (
        b"download:https://pruning.example.invalid/d/library/Cold/Old.srt"
    )
    assert not cold_orphan.exists()
    tasks.tree["/library/Cold"][0]["name"] = "Renamed.mkv"
    tasks.tree["/library/Cold"] = [entry for entry in tasks.tree["/library/Cold"] if entry["name"] != "Delete.mkv"]
    tasks.now[0] += 1
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.calls == []
    assert (tmp_path / "Cold" / "Keep.strm").exists()
    assert (tmp_path / "Cold" / "Delete.strm").exists()
    assert not (tmp_path / "Cold" / "Renamed.strm").exists()
    assert tasks.coordinator.trigger(ScanTarget("pruning", mapping_index=1), force_complete=True).accepted
    tasks.queue.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert (tmp_path / "Cold" / "Renamed.strm").exists()
    assert not (tmp_path / "Cold" / "Keep.strm").exists()
    assert not (tmp_path / "Cold" / "Delete.strm").exists()

    original_post = tasks.transport.post_json

    def save_during_listing(url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        """旧任务已经开始后发布新配置，任务仍使用捕获的播放地址。"""

        response = original_post(url, payload, headers)
        if payload["path"] == "/library" and payload["page"] == 1:
            tasks.configuration["instances"][0]["playback_url"] = "https://play-new.example.invalid"
        return response

    tasks.transport.post_json = save_during_listing
    tasks.now[0] += 1
    tasks.transport.calls.clear()
    assert tasks.coordinator.trigger(ScanTarget("pruning", mapping_index=1), force_complete=True).accepted
    tasks.queue.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert all(
        path.read_text(encoding="utf-8").startswith("https://play-old.example.invalid/")
        for path in tmp_path.rglob("*.strm")
    )
    tasks.transport.post_json = original_post
    retry_orphans = [tmp_path / "orphan.strm", tmp_path / "Cold" / "orphan.strm", deep_orphan]
    for orphan in retry_orphans:
        orphan.write_text("protect stopped mapping", encoding="utf-8")

    class StopPlaybackUpdate(LocalFileSystem):
        """新配置普通任务在首项更新完成后停止。"""

        def replace_text_atomic(self, target: Path, content: str) -> None:
            """部分播放更新不得解除持久化的完整复查要求。"""

            super().replace_text_atomic(target, content)
            assert tasks.coordinator.stop().accepted

    tasks.restart(StopPlaybackUpdate())
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "stopped"
    assert tasks.transport.pages("/library") == [1, 2]
    assert tasks.transport.pages("/library/Cold") == []
    assert (tmp_path / "Film.strm").read_text(encoding="utf-8").startswith("https://play-new.example.invalid/")
    assert (
        (tmp_path / "Cold" / "Renamed.strm").read_text(encoding="utf-8").startswith("https://play-old.example.invalid/")
    )
    assert all(orphan.exists() for orphan in retry_orphans)

    tasks.now[0] += 3600
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.coordinator.status()["state"] == "success"
    assert {path: tasks.transport.pages(path) for path in tasks.tree} == {
        "/library": [1, 2],
        "/library/Cold": [1, 2],
        "/library/Cold/Deep": [1],
        "/library/Warm": [1],
    }
    assert all(not orphan.exists() for orphan in retry_orphans)
    assert all(
        path.read_text(encoding="utf-8").startswith("https://play-new.example.invalid/")
        for path in tmp_path.rglob("*.strm")
    )
    assert len(list(tmp_path.rglob("*.strm"))) == 4
    assert (tmp_path / "Cold" / "Old.srt").exists()
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.calls == []


def test_stability_metadata_order_directory_times_and_same_age_rename(tmp_path: Path) -> None:
    """顺序和子目录时间不算变化；同时间改名及非白名单文件增删都重新建立基线。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"].append({"name": "Season", "is_dir": True, "modified": "2024-01-01T00:00:00Z"})
    tasks.tree["/library/Season"] = []
    tasks.run()
    _stabilize_except(tasks)
    identity = ("pruning", "/library", str(tmp_path))

    def observation() -> Any:
        """从持久边界恢复本层状态。"""

        return DirectoryObservationStore(lambda: tasks.persisted["value"], lambda _value: None).directory(
            identity, "/library"
        )

    assert observation().stable_seconds == 7 * 86400
    tasks.tree["/library"].reverse()
    tasks.tree["/library"][0]["modified"] = "2099-01-01T00:00:00Z"
    tasks.now[0] += 3600
    assert tasks.coordinator.trigger(force_complete=True).accepted
    tasks.queue.run()
    assert observation().stable_seconds == 7 * 86400 + 3600
    tasks.tree["/library"][1]["name"] = "Renamed.mkv"
    tasks.now[0] += 3600
    assert tasks.coordinator.trigger(force_complete=True).accepted
    tasks.queue.run()
    assert observation().stable_seconds == 0
    assert (tmp_path / "Renamed.strm").exists()
    assert not (tmp_path / "Film.strm").exists()
    tasks.now[0] += 86400
    tasks.run()
    assert observation().stable_seconds == 86400
    tasks.tree["/library"].append({"name": "note.txt", "is_dir": False})
    tasks.now[0] += 86400
    tasks.run()
    assert observation().stable_seconds == 0


def test_failed_interval_is_not_counted_as_observed_stability(tmp_path: Path) -> None:
    """跨重启失败中断稳定累计，成功恢复保留失败前时长而不包含停顿。"""

    tasks = _PruningTasks(tmp_path)
    tasks.run()
    _stabilize_except(tasks)
    old = tasks.tree.pop("/library")
    tasks.now[0] += 3600
    assert tasks.coordinator.trigger(force_complete=True).accepted
    tasks.queue.run()
    assert tasks.coordinator.status().state == "failed"
    failed_at = tasks.now[0]
    tasks.tree["/library"] = old
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.now[0] += 3599
    assert not tasks.coordinator.trigger(only_due=True).accepted
    assert tasks.transport.calls == []
    tasks.now[0] = failed_at + 10 * 86400
    tasks.run()
    store = DirectoryObservationStore(lambda: tasks.persisted["value"], lambda _value: None)
    observed = store.directory(("pruning", "/library", str(tmp_path)), "/library")
    assert observed.stable_seconds == 7 * 86400
    assert observed.recheck_at == tasks.now[0] + 7 * 86400


def test_authentication_retry_survives_restart_and_force_bypasses_wait(tmp_path: Path) -> None:
    """认证失败阻止同实例其他映射，重启仍等待，强制全量可以立即恢复。"""

    tasks = _PruningTasks(tmp_path / "first")
    tasks.configuration["instances"][0]["mappings"].append(
        {"source_dir": "/second", "output_dir": str(tmp_path / "second")}
    )
    tasks.tree["/second"] = []
    original_post = tasks.transport.post_json
    attempts: list[str] = []

    def unauthorized(url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        """记录拒绝请求，避免传输内重试影响实例退避断言。"""

        attempts.append(payload["path"])
        return HttpResponse(403, {"code": 403})

    tasks.transport.post_json = unauthorized
    tasks.run()
    assert attempts == ["/library"]
    tasks.restart()
    tasks.now[0] += 60
    assert not tasks.coordinator.trigger(only_due=True).accepted
    assert attempts == ["/library"]
    tasks.transport.post_json = original_post
    assert tasks.coordinator.trigger(force_complete=True).accepted
    tasks.queue.run()
    assert tasks.coordinator.status().state == "success"
    assert tasks.transport.pages("/library") == [1]
    assert tasks.transport.pages("/second") == [1]


def test_legacy_observations_rebuild_without_replacing_existing_products(tmp_path: Path) -> None:
    """旧版本事实不换算稳定时长，迁移重建基线且沿用已有产物规则。"""

    tasks = _PruningTasks(tmp_path)
    tasks.run()
    payload = tasks.persisted["value"]
    assert isinstance(payload, dict)
    payload["version"] = 1
    product = tmp_path / "Film.strm"
    product.write_text("user content", encoding="utf-8")
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.pages("/library") == [1]
    assert product.read_text(encoding="utf-8") == "user content"
    store = DirectoryObservationStore(lambda: tasks.persisted["value"], lambda _value: None)
    observation = store.directory(("pruning", "/library", str(tmp_path)), "/library")
    assert observation.stable_seconds == 0
    assert observation.recheck_at == tasks.now[0] + 3600


def test_scheduled_no_due_work_does_not_open_client_or_write_history(tmp_path: Path) -> None:
    """分钟唤醒只筛选期限，无到期工作时不生成后台任务或历史。"""

    tasks = _PruningTasks(tmp_path)
    history: list[object] = []
    tasks.coordinator._history_callback = history.append
    tasks.run()
    before = len(history)
    tasks.transport.calls.clear()
    for _ in range(59):
        tasks.now[0] += 60
        assert not tasks.coordinator.trigger(only_due=True).accepted
    assert not tasks.queue.jobs
    assert len(history) == before
    assert tasks.transport.calls == []
    tasks.now[0] += 60
    assert tasks.coordinator.trigger(only_due=True).accepted
    tasks.queue.run()
    assert tasks.transport.pages("/library") == [1]


def test_partial_configuration_recovery_completes_without_rewriting_successful_sibling(tmp_path: Path) -> None:
    """配置迁移局部失败后，成功层不重读，所有层恢复后解除映射播放更新要求。"""

    tasks = _PruningTasks(tmp_path)
    tasks.tree["/library"] = [{"name": "Good", "is_dir": True}, {"name": "Bad", "is_dir": True}]
    for branch in ("Good", "Bad"):
        tasks.tree[f"/library/{branch}"] = [{"name": "Film.mkv", "is_dir": False}]
    tasks.run()
    tasks.configuration["instances"][0]["playback_url"] = "https://play-new.example.invalid"
    original_post = tasks.transport.post_json

    def broken(url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        """局部目录返回不完整列表，不触发实例认证失败。"""

        if payload["path"] == "/library/Bad":
            return HttpResponse(200, {"code": 200, "data": {"content": [], "total": 1}})
        return original_post(url, payload, headers)

    tasks.transport.post_json = broken
    tasks.restart()
    tasks.run()
    assert tasks.coordinator.status().state == "partial_failure"
    tasks.transport.post_json = original_post
    # 成功层再次检查确认稳定一天，失败层恢复仍不计失败时长。
    tasks.now[0] += 86400
    tasks.run()
    tasks.transport.calls.clear()
    tasks.now[0] += 3600
    tasks.restart()
    tasks.run()
    assert tasks.transport.pages("/library/Good") == []
    assert tasks.transport.pages("/library/Bad") == [1]
    store = DirectoryObservationStore(lambda: tasks.persisted["value"], lambda _value: None)
    configuration = store.mapping_configuration(("pruning", "/library", str(tmp_path)))
    assert not configuration.needs_check
    assert not configuration.playback_needs_update


def test_clock_rollback_rebuilds_stability_without_waiting_for_future_deadline(tmp_path: Path) -> None:
    """时钟回拨不能长期跳过目录，也不能保留来自未来的稳定跨度。"""

    tasks = _PruningTasks(tmp_path)
    tasks.run()
    _stabilize_except(tasks)
    tasks.now[0] -= 86400
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.run()
    assert tasks.transport.pages("/library") == [1]
    observed = DirectoryObservationStore(lambda: tasks.persisted["value"], lambda _value: None).directory(
        ("pruning", "/library", str(tmp_path)), "/library"
    )
    assert observed.stable_seconds == 0


def test_slow_listing_failure_waits_full_active_interval_before_retry(tmp_path: Path) -> None:
    """耗时超过活跃间隔的失败请求，结束后仍等待完整间隔再重试。"""

    tasks = _PruningTasks(tmp_path)
    tasks.run()
    original_post = tasks.transport.post_json

    def slow_failure(url: str, payload: dict[str, Any], headers: dict[str, str]) -> HttpResponse:
        """返回不可重试的不完整分页，并推进任务时钟两小时。"""

        tasks.now[0] += 7200
        return HttpResponse(200, {"code": 200, "data": {"content": [], "total": 1}})

    tasks.now[0] += 3600
    tasks.transport.post_json = slow_failure
    tasks.run()
    assert tasks.coordinator.status().state == "failed"
    tasks.transport.post_json = original_post
    tasks.restart()
    tasks.transport.calls.clear()
    tasks.now[0] += 3599
    assert not tasks.coordinator.trigger(only_due=True).accepted
    tasks.now[0] += 1
    assert tasks.coordinator.trigger(only_due=True).accepted
    tasks.queue.run()
    assert tasks.transport.pages("/library") == [1]


def test_scan_logs_each_directory_failure_with_run_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """多个目录失败均可关联任务与映射，异常中的凭据和签名地址不会进入日志。"""

    configuration = _config(tmp_path)
    queue = _Queue()
    messages: list[str] = []
    for level in ("info", "warning", "debug", "error"):
        monkeypatch.setattr(scan_module.logger, level, messages.append)

    class Reader:
        """为每个源目录返回包含敏感信息的失败。"""

        def list_dir(self, path: str) -> list[RemoteEntry]:
            """模拟外部边界异常，验证安全日志而不访问网络。"""

            raise OSError(f"{path}: token-a token-b https://user:password@example.invalid/file?sign=private-sign\n伪造日志")

    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=queue,
        client_factory=lambda _url, _token: Reader(),
        media_suffix_provider=lambda: (".mkv",),
    )
    assert coordinator.trigger().accepted
    queue.run()
    run_id = coordinator.status().run_id
    failures = [message for message in messages if "目录读取失败" in message]
    assert len(failures) == 3
    assert all(run_id in message for message in failures)
    assert any("映射结束" in message for message in messages)
    assert any("结束：状态=failed" in message for message in messages)
    assert all("private-sign" not in message and "user:password" not in message for message in messages)
    assert all("\n" not in message for message in messages)
    assert all("token-a" not in message and "token-b" not in message for message in messages)
