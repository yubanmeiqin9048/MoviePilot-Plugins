"""无 shell unar 参数、失败与取消测试。"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from anyio import Path as AsyncPath

from app.plugins.subtitleassistant.file import ArchiveExtractor
from app.plugins.subtitleassistant.schemas.file import ExtractedSubtitle
from app.plugins.subtitleassistant.schemas.source import DownloadedAsset

pytestmark = pytest.mark.anyio


class _CompletedProcess:
    """模拟已完成的 unar 子进程。"""

    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        """保存子进程完成结果。"""

        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        """返回预设标准输出和错误输出。"""

        return self._stdout, self._stderr


class _BlockingProcess:
    """模拟等待显式终止的 unar 子进程。"""

    def __init__(self) -> None:
        """创建阻塞通信状态。"""

        self.returncode: int | None = None
        self.started = asyncio.Event()
        self.released = asyncio.Event()
        self.terminated = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        """等待 terminate 后返回终止错误。"""

        self.started.set()
        await self.released.wait()
        return b"", b"terminated"

    def terminate(self) -> None:
        """记录终止并唤醒通信。"""

        self.terminated = True
        self.returncode = -15
        self.released.set()

    async def wait(self) -> int:
        """记录等待并返回终止码。"""

        self.waited = True
        return self.returncode or 0


async def _write(path: Path, content: bytes = b"archive") -> None:
    """异步创建测试文件。"""

    target = AsyncPath(path)
    await target.parent.mkdir(parents=True, exist_ok=True)
    await target.write_bytes(content)


async def test_extract_invokes_unar_without_shell_and_filters_recursive_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归档使用固定 unar 参数并只返回递归发现的允许字幕格式。"""

    archive = tmp_path / "download" / "Show.S01.1080p.tar.gz"
    output = tmp_path / "extract"
    await _write(archive)
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _CompletedProcess:
        """记录无 shell 参数并模拟解包输出。"""

        calls.append((args, kwargs))
        await _write(output / "nested" / "episode.srt", b"subtitle")
        await _write(output / "nested" / "ignored.txt", b"ignored")
        return _CompletedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    extractor = ArchiveExtractor()

    files = await extractor.extract(
        DownloadedAsset(path=archive, file_name="Show.S01.1080p.tar.gz"),
        output,
        {".srt", "ASS"},
    )

    assert files == [
        ExtractedSubtitle(
            physical_path=Path(output / "nested" / "episode.srt").resolve(),
            logical_source_path=Path("Show.S01.1080p/nested/episode.srt"),
            is_direct_file=False,
        )
    ]
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (
        "unar",
        "-quiet",
        "-force-overwrite",
        "-output-directory",
        str(output),
        str(archive),
    )
    assert kwargs == {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }


async def test_extract_returns_direct_subtitle_without_spawning_unar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已是允许字幕格式的下载文件直接返回，不启动子进程。"""

    subtitle = tmp_path / "candidate.SRT"
    await _write(subtitle, b"subtitle")

    async def fail_if_called(*_args: Any, **_kwargs: Any) -> None:
        """禁止直接字幕路径误调用 unar。"""

        raise AssertionError("不应调用 unar")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_called)

    files = await ArchiveExtractor().extract(
        DownloadedAsset(path=subtitle, file_name=subtitle.name),
        tmp_path / "extract",
        {"srt"},
    )

    assert files == [
        ExtractedSubtitle(
            physical_path=subtitle.resolve(),
            logical_source_path=Path("candidate.SRT"),
            is_direct_file=True,
        )
    ]


async def test_extract_reports_sanitized_unar_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unar 非零退出转为有限长度异常且不遍历伪结果。"""

    archive = tmp_path / "package.rar"
    await _write(archive)
    detail = ("x" * 400 + " final failure").encode()

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> _CompletedProcess:
        """返回失败的 unar 进程。"""

        return _CompletedProcess(returncode=2, stderr=detail)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="unar 解包失败") as exc_info:
        await ArchiveExtractor().extract(
            DownloadedAsset(path=archive, file_name=archive.name),
            tmp_path / "extract",
            {"srt"},
        )

    assert "final failure" in str(exc_info.value)
    assert len(str(exc_info.value)) <= 320


async def test_cancel_terminates_and_waits_for_active_unar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消解包会 terminate 当前进程并等待其退出。"""

    archive = tmp_path / "package.7z"
    await _write(archive)
    process = _BlockingProcess()

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> _BlockingProcess:
        """返回等待取消的 unar 进程。"""

        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    extractor = ArchiveExtractor()
    task = asyncio.create_task(
        extractor.extract(
            DownloadedAsset(path=archive, file_name=archive.name),
            tmp_path / "extract",
            {"srt"},
        )
    )
    await process.started.wait()

    await extractor.cancel()

    assert process.terminated
    assert process.waited
    with pytest.raises(RuntimeError, match="unar 解包失败"):
        await task


async def test_extract_recurses_nested_archives_and_continues_failed_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外层包包含多个内层包时递归汇总字幕，单个分支失败不影响其他分支。"""
    archive = tmp_path / "root.zip"
    await _write(archive)
    calls: list[str] = []

    async def fake_unar(*args: Any, **kwargs: Any) -> _CompletedProcess:
        """按归档名称创建带原始目录的嵌套测试产物。"""

        source = Path(args[-1])
        calls.append(source.name)
        out = Path(args[args.index("-output-directory") + 1])
        if source.name == "root.zip":
            for relative in ("bundles/a.zip", "bundles/b.zip", "broken/bad.zip"):
                await _write(out / relative, relative.encode())
        elif source.name == "a.zip":
            await _write(out / "Season 01" / "a.srt")
        elif source.name == "b.zip":
            await _write(out / "Season 02" / "b.ass")
        else:
            return _CompletedProcess(returncode=2, stderr=b"bad branch")
        return _CompletedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_unar)
    files = await ArchiveExtractor().extract(
        DownloadedAsset(path=archive, file_name=archive.name), tmp_path / "out", {"srt", "ass"}
    )
    assert {item.physical_path.name for item in files} == {"a.srt", "b.ass"}
    assert {item.logical_source_path for item in files} == {
        Path("root/bundles/Season 01/a.srt"),
        Path("root/bundles/Season 02/b.ass"),
    }
    assert all(not item.is_direct_file for item in files)
    assert all("level_" not in str(item.logical_source_path) for item in files)
    assert all(str(tmp_path) not in str(item.logical_source_path) for item in files)
    assert calls[0] == "root.zip"
    assert set(calls[1:]) == {"a.zip", "b.zip", "bad.zip"}


async def test_extract_enforces_depth_and_archive_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """达到深度或数量上限时保留已提取字幕并停止继续展开。"""
    archive = tmp_path / "root.zip"
    await _write(archive)

    async def fake_unar(*args: Any, **kwargs: Any) -> _CompletedProcess:
        source = Path(args[-1])
        out = Path(args[args.index("-output-directory") + 1])
        if source.name.startswith("deep"):
            await _write(out / "deep.srt")
            await _write(out / "next.zip")
        else:
            await _write(out / "deep1.zip", b"deep-1")
            await _write(out / "root.srt")
        return _CompletedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_unar)
    files = await ArchiveExtractor().extract(
        DownloadedAsset(path=archive, file_name=archive.name), tmp_path / "out", {"srt"}
    )
    assert {item.physical_path.name for item in files} == {"root.srt", "deep.srt"}
    assert {item.logical_source_path for item in files} == {
        Path("root/root.srt"),
        Path("root/deep.srt"),
    }


async def test_extract_deduplicates_same_archive_fingerprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一归档路径和内容指纹只展开一次。"""
    archive = tmp_path / "root.zip"
    await _write(archive)
    calls = 0

    async def fake_unar(*args: Any, **kwargs: Any) -> _CompletedProcess:
        nonlocal calls
        calls += 1
        out = Path(args[args.index("-output-directory") + 1])
        await _write(out / "same.srt")
        return _CompletedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_unar)
    files = await ArchiveExtractor().extract(
        DownloadedAsset(path=archive, file_name=archive.name), tmp_path / "out", {"srt"}
    )
    assert len(files) == 1
    assert calls == 1


async def test_extract_stops_after_one_hundred_archives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单个候选最多展开一百个归档。"""
    archive = tmp_path / "root.zip"
    await _write(archive)
    calls = 0

    async def fake_unar(*args: Any, **kwargs: Any) -> _CompletedProcess:
        nonlocal calls
        calls += 1
        out = Path(args[args.index("-output-directory") + 1])
        await _write(out / f"next{calls}.zip", f"archive-{calls}".encode())
        return _CompletedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_unar)
    extractor = ArchiveExtractor()
    extractor._max_depth = 200
    await extractor.extract(DownloadedAsset(path=archive, file_name=archive.name), tmp_path / "out", {"srt"})
    assert calls == 100
