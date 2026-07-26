"""插件非敏感配置解析与公开模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.enums import PackageAttributionStrategy, SubtitleSource
from ..domain.language import normalize_format_priority
from .path_mapping import PathMapping, validate_path_mappings


@dataclass(slots=True)
class PluginConfig:
    """字幕助手运行所需的非敏感配置。"""

    enabled: bool = False
    moviepilot_enabled: bool = True
    opensubtitles_enabled: bool = False
    assrt_enabled: bool = False
    allow_machine_translation: bool = False
    max_candidate_attempts: int = 3
    source_priority: list[str] = field(default_factory=lambda: ["moviepilot", "assrt", "opensubtitles"])
    format_priority: list[str] = field(default_factory=list)
    path_mappings: tuple[PathMapping, ...] = field(default_factory=tuple)
    package_attribution_strategy: PackageAttributionStrategy = PackageAttributionStrategy.TRUST_PACKAGE
    # 字幕归属 AI 接管是独立授权，默认关闭；宿主 AI 总开关在运行时再复核。
    ai_attribution_takeover_enabled: bool = False

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None, allowed_formats: list[str]) -> PluginConfig:
        """从宿主配置解析并归一化运行配置。"""

        values = raw or {}
        source_order: list[str] = []
        for item in values.get("source_priority") or ["moviepilot", "assrt", "opensubtitles"]:
            normalized = str(item).strip().lower()
            if normalized in {source.value for source in SubtitleSource} and normalized not in source_order:
                source_order.append(normalized)
        source_order.extend(source.value for source in SubtitleSource if source.value not in source_order)
        try:
            attempts = int(values.get("max_candidate_attempts", 3))
        except (TypeError, ValueError):
            attempts = 3
        strategy = PackageAttributionStrategy(
            str(
                values.get(
                    "package_attribution_strategy",
                    PackageAttributionStrategy.TRUST_PACKAGE.value,
                )
            )
        )
        return cls(
            enabled=bool(values.get("enabled", False)),
            moviepilot_enabled=bool(values.get("moviepilot_enabled", True)),
            opensubtitles_enabled=bool(values.get("opensubtitles_enabled", False)),
            assrt_enabled=bool(values.get("assrt_enabled", False)),
            allow_machine_translation=bool(values.get("allow_machine_translation", False)),
            max_candidate_attempts=min(10, max(1, attempts)),
            source_priority=source_order,
            format_priority=normalize_format_priority(allowed_formats, values.get("format_priority")),
            path_mappings=validate_path_mappings(values.get("path_mappings")),
            package_attribution_strategy=strategy,
            ai_attribution_takeover_enabled=bool(values.get("ai_attribution_takeover_enabled", False)),
        )

    def enabled_sources(self) -> dict[SubtitleSource, bool]:
        """返回三个来源的启用状态。"""

        return {
            SubtitleSource.MOVIEPILOT: self.moviepilot_enabled,
            SubtitleSource.OPENSUBTITLES: self.opensubtitles_enabled,
            SubtitleSource.ASSRT: self.assrt_enabled,
        }

    def saved_payload(self) -> dict[str, Any]:
        """返回可以交给宿主保存的完整非敏感配置。"""

        return {
            "enabled": self.enabled,
            "moviepilot_enabled": self.moviepilot_enabled,
            "opensubtitles_enabled": self.opensubtitles_enabled,
            "assrt_enabled": self.assrt_enabled,
            "allow_machine_translation": self.allow_machine_translation,
            "max_candidate_attempts": self.max_candidate_attempts,
            "source_priority": list(self.source_priority),
            "format_priority": list(self.format_priority),
            "path_mappings": [mapping.as_dict() for mapping in self.path_mappings],
            "package_attribution_strategy": self.package_attribution_strategy.value,
            "ai_attribution_takeover_enabled": self.ai_attribution_takeover_enabled,
        }

    def public_payload(
        self,
        plugin_id: str,
        allowed_formats: list[str],
        opensubtitles_configured: bool,
        assrt_configured: bool,
        host_ai_enabled: bool = False,
    ) -> dict[str, Any]:
        """返回 Vue Config 使用且不含秘密的初始模型。"""

        return {
            "plugin_id": plugin_id,
            **self.saved_payload(),
            "opensubtitles_configured": opensubtitles_configured,
            "assrt_configured": assrt_configured,
            "host_ai_enabled": bool(host_ai_enabled),
            "allowed_formats": [str(item).lstrip(".").upper() for item in allowed_formats],
        }
