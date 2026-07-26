"""第三方字幕源共用的安全 HTTP 与缓存工具。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from anyio import Path as AsyncPath

from app.utils.http import AsyncRequestUtils

from ..application.ports import CandidateHandle
from ..domain.models import SubtitleCandidate


class SourceRequestError(RuntimeError):
    """不包含响应原文和敏感字段的字幕源请求错误。"""


class SourceLimitedError(SourceRequestError):
    """字幕源明确返回限流时抛出的安全错误。"""

    def __init__(self, message: str, retry_at: datetime | None = None) -> None:
        """保存可选的预计恢复时间。"""

        super().__init__(message)
        self.retry_at = retry_at


def _proxy_kwargs(proxies: dict[str, str] | None) -> dict[str, Any]:
    """透传宿主实际支持、但其旧式类型注解未声明可空的代理参数。"""

    return {"proxies": proxies}


def cache_key(source: str, payload: dict[str, Any]) -> str:
    """为结构化查询参数生成稳定且不可逆的缓存键。"""

    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"{source}:{digest}"


def encode_handles(handles: list[CandidateHandle]) -> list[dict[str, Any]]:
    """把安全候选与仅服务端内存存在的不透明下载句柄编码为缓存值。"""

    return [
        {
            "candidate": item.candidate.model_dump(mode="json"),
            "opaque": item.opaque,
        }
        for item in handles
    ]


def decode_handles(values: list[dict[str, Any]]) -> list[CandidateHandle]:
    """从缓存恢复安全候选与仅服务端内存存在的不透明下载句柄。"""

    return [
        CandidateHandle(
            candidate=SubtitleCandidate.model_validate(item["candidate"]),
            opaque=item["opaque"],
        )
        for item in values
    ]


def encode_candidate_pool(
    handles: list[CandidateHandle],
    raw_count: int,
    rejection_summary: dict[str, int] | None = None,
    page_count: int = 1,
    pagination_complete: bool = True,
) -> dict[str, Any]:
    """把来源候选池及安全查询摘要编码为可缓存值。"""

    return {
        "handles": encode_handles(handles),
        "raw_count": raw_count,
        "rejection_summary": dict(rejection_summary or {}),
        "page_count": page_count,
        "pagination_complete": pagination_complete,
        "stored_at": datetime.now(UTC).isoformat(),
    }


def decode_candidate_pool(value: Any) -> tuple[list[CandidateHandle], dict[str, Any]] | None:
    """从缓存恢复来源候选池，兼容正常空结果。"""

    if not isinstance(value, dict) or not isinstance(value.get("handles"), list):
        return None
    details = {
        "raw_count": int(value.get("raw_count") or 0),
        "rejection_summary": {str(key): int(count) for key, count in (value.get("rejection_summary") or {}).items()},
        "page_count": max(1, int(value.get("page_count") or 1)),
        "pagination_complete": bool(value.get("pagination_complete", True)),
        "cache_stored_at": value.get("stored_at"),
    }
    return decode_handles(value["handles"]), details


def parse_datetime(value: Any, format_string: str | None = None) -> datetime | None:
    """容错解析第三方来源时间并统一为带时区 UTC。"""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        if format_string:
            if "%z" in format_string:
                result = datetime.strptime(value.strip(), format_string).astimezone(UTC)
            else:
                result = datetime.strptime(value.strip(), format_string).replace(tzinfo=UTC)
        else:
            result = datetime.fromisoformat(value.strip())
        if result.tzinfo is None:
            result = result.replace(tzinfo=UTC)
        return result.astimezone(UTC)
    except ValueError:
        return None


def safe_file_name(value: str | None, fallback: str) -> str:
    """移除第三方文件名中的目录部分并提供安全后备名。"""

    name = Path(value or "").name.strip()
    return name or fallback


async def download_file(
    request: AsyncRequestUtils,
    url: str,
    directory: Path,
    file_name: str,
    prefer_response_name: bool = False,
) -> Path:
    """通过 AsyncRequestUtils 流式下载到排他创建的临时文件。"""

    destination: AsyncPath | None = None
    try:
        async with request.get_stream(url=url) as response:
            if response is None:
                raise SourceRequestError("字幕文件请求失败")
            if response.status_code >= 400:
                raise SourceRequestError(f"字幕文件请求返回 HTTP {response.status_code}")
            selected_name = file_name
            if prefer_response_name:
                response_url = str(getattr(response, "url", "") or "")
                selected_name = response_file_name(
                    getattr(response, "headers", {}),
                    response_url or url,
                    response_file_name({}, url, file_name),
                )
            destination = AsyncPath(directory / safe_file_name(selected_name, "subtitle.bin"))
            await destination.parent.mkdir(parents=True, exist_ok=True)
            async with await destination.open("xb") as output:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    if chunk:
                        await output.write(chunk)
    except BaseException:
        if destination is not None and await destination.exists():
            await destination.unlink()
        raise
    if destination is None:
        raise SourceRequestError("字幕文件请求未产生下载文件")
    return Path(destination)


def response_file_name(headers: Any, url: str, fallback: str) -> str:
    """优先从响应头、其次从下载地址推导安全文件名。"""

    disposition = str(headers.get("content-disposition") or "") if headers is not None else ""
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.IGNORECASE)
    if encoded:
        return safe_file_name(unquote(encoded.group(1).strip()), fallback)
    quoted = re.search(r'filename="([^"]+)"', disposition, flags=re.IGNORECASE)
    if quoted:
        return safe_file_name(unquote(quoted.group(1).strip()), fallback)
    plain = re.search(r"filename=([^;]+)", disposition, flags=re.IGNORECASE)
    if plain:
        return safe_file_name(unquote(plain.group(1).strip().strip('"')), fallback)
    parsed_path = urlparse(url).path
    url_name = "" if parsed_path.endswith("/") else unquote(Path(parsed_path).name)
    return safe_file_name(url_name, fallback)


def subtitle_format(file_name: str | None, subtype: str | None = None) -> str:
    """从文件名或来源格式名称归一出宿主扩展名。"""

    suffix = Path(file_name or "").suffix.lstrip(".").upper()
    if suffix:
        return suffix
    normalized = (subtype or "").strip().lower()
    aliases = {
        "subrip": "SRT",
        "srt": "SRT",
        "advanced substation alpha": "ASS",
        "ass": "ASS",
        "substation alpha": "SSA",
        "ssa": "SSA",
        "sup": "SUP",
        "pgs": "SUP",
    }
    return aliases.get(normalized, normalized.upper())
