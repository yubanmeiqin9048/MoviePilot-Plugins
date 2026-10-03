"""手动扫描高层入口的 OpenList、文件系统和任务控制契约测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.plugins.alist2strm import scan as scan_module  # ty: ignore[unresolved-import]
from app.plugins.alist2strm.config import build_configuration  # ty: ignore[unresolved-import]
from app.plugins.alist2strm.scan import (  # ty: ignore[unresolved-import]
    DownloadResponse,
    HttpResponse,
    LocalFileSystem,
    OpenListClient,
    OpenListError,
    RemoteEntry,
    ScanCoordinator,
    build_playback_url,
)


class _Transport:
    """按目录树分页返回受控 OpenList 响应，并记录认证边界。"""

    def __init__(self, trees: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        """保存多个实例的目录树。"""

        self.trees = trees
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
        return HttpResponse(
            200,
            {
                "code": 200,
                "data": {"content": entries[start : start + per_page], "total": len(entries)},
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


def test_playback_address_change_updates_existing_video_target(tmp_path: Path) -> None:
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
    assert coordinator.trigger().accepted is True
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


def test_scan_shutdown_closes_client_resources() -> None:
    """停止服务时已创建的客户端最终释放资源。"""

    configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "release",
                    "name": "释放实例",
                    "scan_url": "https://release.example.invalid",
                    "api_token": "release-token",
                    "mappings": [{"source_dir": "/library", "output_dir": "/tmp/release-output"}],
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


class _EmptyReader:
    """清理测试使用的空源目录替身。"""

    def list_dir(self, path: str) -> list[RemoteEntry]:
        """返回空目录。"""

        return []


def test_cleanup_delete_failure_continues_and_marks_run_failed(tmp_path: Path) -> None:
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
