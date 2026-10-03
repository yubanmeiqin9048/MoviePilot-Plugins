"""手动扫描高层入口的 OpenList、文件系统和任务控制契约测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from app.plugins.alist2strm.config import build_configuration  # ty: ignore[unresolved-import]
from app.plugins.alist2strm.scan import (  # ty: ignore[unresolved-import]
    HttpResponse,
    OpenListClient,
    ScanCoordinator,
    build_playback_url,
)


class _Transport:
    """按目录树分页返回受控 OpenList 响应，并记录认证边界。"""

    def __init__(self, trees: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        """保存多个实例的目录树。"""

        self.trees = trees
        self.calls: list[dict[str, Any]] = []

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

    def pages(self, path: str) -> list[int]:
        """返回目录请求的页码。"""

        return [int(call["payload"]["page"]) for call in self.calls if call["payload"]["path"] == path]


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


def test_scan_entry_generates_relative_video_strm_with_pages_and_safe_urls(tmp_path: Path) -> None:
    """多实例、多映射、深层路径和分页只生成视频 STRM，并跳过既有目标。"""

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
    assert orphan.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "a" / "notes.strm").exists()
    assert transport.pages("/media/movies") == [1, 2, 3]

    status = coordinator.status()
    assert status["state"] == "success"
    assert status["current_mapping"] is None
    assert status["counters"] == {
        "generated": 3,
        "downloaded": 0,
        "updated": 0,
        "skipped": 1,
        "deleted": 0,
        "failed": 0,
    }
    assert status["total_mappings"] == 3
    assert status["mappings"][1]["state"] == "success"  # type: ignore[index]
    for call in transport.calls:
        assert call["headers"] == {"Authorization": "token-a"} or call["headers"] == {"Authorization": "token-b"}
        assert "Bearer" not in str(call["headers"])
        assert "token" not in str(call["payload"]).casefold()


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

    assert coordinator.trigger().accepted is True
    queue.run()

    status = coordinator.status()
    assert status["state"] == "failed"
    assert status["counters"]["generated"] == 1  # type: ignore[index]
    assert status["counters"]["failed"] == 1  # type: ignore[index]
    assert status["mappings"][1]["state"] == "success"  # type: ignore[index]
    assert status["mappings"][2]["state"] == "failed"  # type: ignore[index]
    assert (tmp_path / "a" / "ok.strm").is_file()
    assert not (tmp_path / "empty").exists()


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


def test_playback_url_encodes_each_path_segment_without_double_encoding() -> None:
    """播放基地址只拼一次，特殊字符按段编码且不写入签名。"""

    assert build_playback_url("https://play.example.invalid/", "/A B/%?#/剧.mkv") == (
        "https://play.example.invalid/d/A%20B/%25%3F%23/%E5%89%A7.mkv"
    )
    assert build_playback_url("https://user:password@play.example.invalid/base?token=secret", "/movie.mkv") == (
        "https://play.example.invalid/base/d/movie.mkv"
    )
