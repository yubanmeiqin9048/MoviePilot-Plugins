"""字幕源查询词领域规则。"""

from __future__ import annotations

from .models import MediaContext


def assrt_title_queries(context: MediaContext) -> tuple[str | None, str | None]:
    """返回 ASSRT 主标题与非重复备选标题两个固定查询槽位。"""

    primary = context.title.strip()
    alternate = next(
        (
            item.strip()
            for item in (context.english_title, context.original_title)
            if isinstance(item, str) and item.strip() and item.strip().casefold() != primary.casefold()
        ),
        "",
    )
    return (
        primary if len(primary) >= 3 else None,
        alternate if len(alternate) >= 3 else None,
    )
