"""自动准入纯函数的契约测试：语言/翻译判定与精确身份标记。"""

from __future__ import annotations

from pathlib import Path

from app.plugins.subtitleassistant.candidate import admit_automatic_candidates
from app.plugins.subtitleassistant.schemas.candidate import (
    SubtitleCandidate,
    TranslationType,
)
from app.plugins.subtitleassistant.schemas.source import (
    AssrtDownloadHandle,
    CandidateHandle,
    OpenSubtitlesDownloadHandle,
    SubtitleSource,
)
from app.plugins.subtitleassistant.schemas.target import MediaType, SubtitleTarget


def _context() -> SubtitleTarget:
    """构造带 TMDB/IMDb 身份的自动搜索目标。"""

    return SubtitleTarget(
        title="中文标题",
        english_title="English Title",
        year=2026,
        media_type=MediaType.TV,
        season=2,
        episode=3,
        tmdb_id=1234,
        imdb_id="tt0012345",
        target_path=Path("/media/Show.S02E03.mkv"),
        target_file_name="Show.S02E03.mkv",
        target_storage="local",
    )


def _handle(
    key: str,
    source: SubtitleSource,
    *,
    language: str = "zh-CN",
    translation_type: TranslationType = TranslationType.HUMAN,
    foreign_parts_only: bool = False,
    tmdb_id: int | None = None,
    imdb_id: str | None = None,
) -> CandidateHandle:
    """构造一个来源候选并配对下载句柄。"""

    candidate = SubtitleCandidate(
        candidate_key=key,
        source=source,
        name=key,
        language=language,
        translation_type=translation_type,
        foreign_parts_only=foreign_parts_only,
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
    )
    if source is SubtitleSource.OPENSUBTITLES:
        handle = OpenSubtitlesDownloadHandle(file_id=1)
    else:
        handle = AssrtDownloadHandle(subtitle_id=1)
    return CandidateHandle(candidate=candidate, download_handle=handle)


def test_pure_admission_rejects_non_chinese_and_machine_translation_with_reasons() -> None:
    """准入纯函数按语言与翻译类型过滤，并按原因归类排除计数。"""

    accepted = _handle("accepted", SubtitleSource.OPENSUBTITLES, language="zh-cn")
    english = _handle("english", SubtitleSource.OPENSUBTITLES, language="en")
    machine = _handle(
        "machine",
        SubtitleSource.OPENSUBTITLES,
        language="zh-cn",
        translation_type=TranslationType.MACHINE,
    )
    foreign_parts = _handle(
        "foreign-parts",
        SubtitleSource.OPENSUBTITLES,
        language="zh-cn",
        foreign_parts_only=True,
    )

    admitted, rejected = admit_automatic_candidates(
        [accepted, english, machine, foreign_parts],
        _context(),
        allow_machine_translation=False,
    )

    assert [handle.candidate.candidate_key for handle in admitted] == ["accepted"]
    assert rejected == {"language": 1, "machine_translation": 1, "foreign_parts_only": 1}


def test_pure_admission_marks_exact_media_identity_for_ranking() -> None:
    """准入纯函数为通过候选标记与当前目标一致的精确媒体身份。"""

    exact_tmdb = _handle("exact-tmdb", SubtitleSource.OPENSUBTITLES, language="zh-cn", tmdb_id=1234)
    exact_imdb = _handle("exact-imdb", SubtitleSource.OPENSUBTITLES, language="zh-cn", imdb_id="tt0012345")
    mismatch = _handle("mismatch", SubtitleSource.OPENSUBTITLES, language="zh-cn", tmdb_id=9999)

    admitted, rejected = admit_automatic_candidates(
        [exact_tmdb, exact_imdb, mismatch],
        _context(),
        allow_machine_translation=True,
    )

    assert rejected == {}
    assert [handle.candidate.exact_id_match for handle in admitted] == [True, True, False]


def test_pure_admission_allows_machine_translation_when_configured() -> None:
    """配置允许机器翻译时准入不排除机器与 AI 翻译候选。"""

    machine = _handle(
        "machine",
        SubtitleSource.OPENSUBTITLES,
        language="zh-cn",
        translation_type=TranslationType.MACHINE,
    )
    ai = _handle(
        "ai",
        SubtitleSource.OPENSUBTITLES,
        language="zh-cn",
        translation_type=TranslationType.AI,
    )

    admitted, rejected = admit_automatic_candidates(
        [machine, ai],
        _context(),
        allow_machine_translation=True,
    )

    assert [handle.candidate.candidate_key for handle in admitted] == ["machine", "ai"]
    assert rejected == {}
