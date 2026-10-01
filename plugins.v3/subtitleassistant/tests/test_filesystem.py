"""字幕文件检查、排他落盘与插件目录安全删除测试。"""

from pathlib import Path

import pytest
from anyio import Path as AsyncPath
from app.plugins.subtitleassistant.file import SubtitleFiles
from app.plugins.subtitleassistant.schemas.record import RecordStatus

pytestmark = pytest.mark.anyio


async def _write(path: Path, content: bytes) -> None:
    """异步创建测试文件。"""

    target = AsyncPath(path)
    await target.parent.mkdir(parents=True, exist_ok=True)
    await target.write_bytes(content)


@pytest.mark.parametrize(
    "subtitle_name",
    [
        "Movie.default.chi.zh-cn.srt",
        "Movie.chi.zh-cn.srt",
        "MOVIE.DEFAULT.CHI.ZH-CN.ASS",
        "MOVIE.CHI.ZH-CN.ASS",
    ],
)
async def test_existing_subtitle_accepts_both_strict_standard_names(
    tmp_path: Path,
    subtitle_name: str,
) -> None:
    """已有字幕检查同时接受两个严格同主文件名的标准简中后缀。"""

    target = tmp_path / "Movie.mkv"
    subtitle = tmp_path / subtitle_name
    await _write(target, b"video")
    await _write(subtitle, b"subtitle")
    filesystem = SubtitleFiles(tmp_path / "plugin", {"srt", "ass"})

    found = await filesystem.has_standard_subtitle(target)

    assert found is not None
    assert found.name.casefold() == subtitle.name.casefold()


async def test_existing_subtitle_rejects_loose_or_different_stem_names(tmp_path: Path) -> None:
    """模糊语言名、繁中和其他视频主文件名均不视为已有标准字幕。"""

    target = tmp_path / "Movie.mkv"
    await _write(target, b"video")
    for name in (
        "Movie.zh-cn.srt",
        "Movie.default.zh-cn.srt",
        "Movie.chi.zh-tw.srt",
        "Movie.copy.default.chi.zh-cn.srt",
        "Other.default.chi.zh-cn.srt",
        "Movie.default.chi.zh-cn.vtt",
    ):
        await _write(tmp_path / name, b"subtitle")
    filesystem = SubtitleFiles(tmp_path / "plugin", {"srt", "ass"})

    assert await filesystem.has_standard_subtitle(target) is None


async def test_write_media_subtitle_always_uses_plugin_standard_suffix(tmp_path: Path) -> None:
    """插件字幕落盘固定使用不含 default 的标准简中后缀。"""

    source = tmp_path / "download" / "candidate.srt"
    target = tmp_path / "media" / "Movie.mkv"
    await _write(source, b"new subtitle")
    await _write(target, b"video")
    filesystem = SubtitleFiles(tmp_path / "plugin", {".srt"})

    destination = await filesystem.write_media_subtitle(source, target)

    assert destination == target.with_name("Movie.chi.zh-cn.srt")
    assert await AsyncPath(destination).read_bytes() == b"new subtitle"


async def test_write_media_subtitle_never_overwrites_or_deletes_existing_file(tmp_path: Path) -> None:
    """排他落盘冲突时保留既有字幕原文且不生成冲突副本。"""

    source = tmp_path / "download" / "candidate.srt"
    target = tmp_path / "media" / "Movie.mkv"
    destination = target.with_name("Movie.chi.zh-cn.srt")
    await _write(source, b"new subtitle")
    await _write(target, b"video")
    await _write(destination, b"existing subtitle")
    filesystem = SubtitleFiles(tmp_path / "plugin", {"srt"})

    with pytest.raises(FileExistsError):
        await filesystem.write_media_subtitle(source, target)

    assert await AsyncPath(destination).read_bytes() == b"existing subtitle"
    assert sorted(path.name for path in target.parent.iterdir()) == [
        "Movie.chi.zh-cn.srt",
        "Movie.mkv",
    ]


async def test_plugin_file_save_and_delete_are_confined_to_data_root(tmp_path: Path) -> None:
    """记录文件只能保存和删除于插件数据目录，媒体及目录目标保持不变。"""

    data_root = tmp_path / "plugin-data"
    source = tmp_path / "download" / "candidate.ass"
    media_subtitle = tmp_path / "media" / "Movie.chi.zh-cn.ass"
    await _write(source, b"staged")
    await _write(media_subtitle, b"media subtitle")
    filesystem = SubtitleFiles(data_root, {"ass"})

    relative = await filesystem.save_plugin_file(source, "record-1", RecordStatus.STAGED)
    stored = await filesystem.plugin_file_path(relative)
    assert relative == "staged/record-1.ass"
    assert await AsyncPath(stored).read_bytes() == b"staged"

    await filesystem.delete_plugin_file(relative)
    await filesystem.delete_plugin_file(relative)
    assert not await AsyncPath(stored).exists()

    with pytest.raises(ValueError):
        await filesystem.delete_plugin_file(str(media_subtitle))
    with pytest.raises(ValueError):
        await filesystem.delete_plugin_file("../media/Movie.chi.zh-cn.ass")
    assert await AsyncPath(media_subtitle).read_bytes() == b"media subtitle"

    directory = data_root / "staged" / "directory"
    await AsyncPath(directory).mkdir(parents=True)
    with pytest.raises(IsADirectoryError):
        await filesystem.delete_plugin_file("staged/directory")
    assert await AsyncPath(directory).is_dir()


async def test_plugin_file_paths_reject_symlink_components(tmp_path: Path) -> None:
    """插件数据路径不能借由符号链接读写或删除根目录之外的文件。"""

    data_root = tmp_path / "plugin-data"
    outside = tmp_path / "outside"
    await AsyncPath(data_root).mkdir(parents=True)
    await AsyncPath(outside).mkdir(parents=True)
    outside_file = outside / "escape.srt"
    await AsyncPath(outside_file).write_text("outside")
    link = data_root / "staged"
    await AsyncPath(link).symlink_to(outside, target_is_directory=True)
    source = tmp_path / "candidate.srt"
    await AsyncPath(source).write_text("candidate")
    filesystem = SubtitleFiles(data_root, {"srt"})

    with pytest.raises(ValueError, match="符号链接"):
        await filesystem.plugin_file_path("staged/escape.srt")
    with pytest.raises(ValueError, match="符号链接"):
        await filesystem.save_plugin_file(source, "escape", RecordStatus.STAGED)
    with pytest.raises(ValueError, match="符号链接"):
        await filesystem.delete_plugin_file("staged/escape.srt")
    with pytest.raises(ValueError, match="符号链接"):
        await filesystem.stage_file_deletion(link / "escape.srt")

    assert await AsyncPath(outside_file).read_text() == "outside"


async def test_save_plugin_file_rejects_matched_status(tmp_path: Path) -> None:
    """已匹配记录不得误写入插件暂存或未匹配目录。"""

    source = tmp_path / "candidate.srt"
    await _write(source, b"subtitle")
    filesystem = SubtitleFiles(tmp_path / "plugin", {"srt"})

    with pytest.raises(ValueError, match="暂存或未匹配"):
        await filesystem.save_plugin_file(source, "record-1", RecordStatus.MATCHED)
