"""MoviePilot 字幕助手插件入口。"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from app.core.config import global_vars, settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.schemas.types import ChainEventType

from ..api import ApiController
from ..attribution import FileAttributor
from ..config import load_config, public_config
from ..event import SubtitleEvents
from ..file import ArchiveExtractor, SubtitleFiles
from ..record import RecordCatalog, RecordCommitter, RecordMaintenance
from ..schemas.attribution import CandidateMatchContext
from ..schemas.config import PluginConfig
from ..schemas.source import SourceHealth, SourceStatus, SubtitleSource
from ..schemas.target import MediaType, SubtitleTarget
from ..schemas.task import TaskWorkItem
from ..search import ManualSearch
from ..source import SourceAdministration
from ..store import PluginDataStore, StoreInitializationError
from ..target import TargetCatalog
from ..task import TaskOperations

_PLUGIN_INSTANCES: weakref.WeakValueDictionary[str, PluginRuntime] = weakref.WeakValueDictionary()


class PluginHost(Protocol):
    """组合根使用的最小 MoviePilot 插件宿主接口。"""

    def get_data(self, key: str) -> object:
        """同步读取一个 PluginData 分区。"""

    async def async_get_data(self, key: str) -> object:
        """异步读取一个 PluginData 分区。"""

    def save_data(self, key: str, value: object) -> None:
        """同步保存一个 PluginData 分区。"""

    async def async_save_data(self, key: str, value: object) -> None:
        """异步保存一个 PluginData 分区。"""

    def get_data_path(self) -> Path:
        """返回插件运行数据目录。"""

    def update_config(self, config: dict[str, object], plugin_id: str) -> bool:
        """保存插件公开配置。"""


@eventmanager.register(ChainEventType.PluginDataReset)
def _handle_plugin_data_reset(event: Event) -> None:
    """在宿主删除 PluginData 前同步清理当前插件自有文件。"""

    payload = event.event_data
    plugin_id = getattr(payload, "plugin_id", None)
    if not plugin_id or not bool(getattr(payload, "reset_data", False)):
        return
    runtime = _PLUGIN_INSTANCES.get(str(plugin_id))
    if runtime is not None:
        runtime.reset_data_sync()


def build_media_context(target: Any, meta: Any, mediainfo: Any) -> SubtitleTarget | None:
    """在宿主整理事件边界投影安全的字幕目标。"""

    path_value = getattr(target, "path", None)
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    target_name = str(getattr(target, "name", None) or Path(path_value).name)
    media_title = str(
        getattr(mediainfo, "title", None)
        or getattr(meta, "name", None)
        or getattr(meta, "cn_name", None)
        or getattr(meta, "en_name", None)
        or Path(path_value).stem
    ).strip()
    media_type_value = getattr(getattr(mediainfo, "type", None), "name", None) or str(
        getattr(getattr(mediainfo, "type", None), "value", "")
    )
    media_type = MediaType.TV if media_type_value.upper() in {"TV", "电视剧"} else MediaType.MOVIE
    year_value = getattr(mediainfo, "year", None) or getattr(meta, "year", None)
    try:
        year = int(year_value) if year_value not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    season = getattr(mediainfo, "season", None) or getattr(meta, "begin_season", None)
    episode = getattr(meta, "begin_episode", None)
    try:
        season = int(season) if season is not None else None
    except (TypeError, ValueError):
        season = None
    try:
        episode = int(episode) if episode is not None else None
    except (TypeError, ValueError):
        episode = None
    tmdb_id = getattr(mediainfo, "tmdb_id", None) or getattr(meta, "tmdbid", None)
    try:
        tmdb_id = int(tmdb_id) if tmdb_id not in (None, "") else None
    except (TypeError, ValueError):
        tmdb_id = None
    return SubtitleTarget(
        title=media_title,
        original_title=getattr(mediainfo, "original_title", None),
        english_title=getattr(mediainfo, "en_title", None),
        year=year,
        media_type=media_type,
        season=season,
        episode=episode,
        tmdb_id=tmdb_id,
        imdb_id=getattr(mediainfo, "imdb_id", None),
        target_path=Path(path_value),
        target_file_name=target_name,
        target_storage=getattr(target, "storage", None),
        target_type=str(getattr(target, "type", None) or "file"),
        target_extension=str(getattr(target, "extension", None) or Path(path_value).suffix).lstrip("."),
        target_container=getattr(target, "container", None),
    )


def build_match_context(context: SubtitleTarget, mediainfo: Any) -> CandidateMatchContext | None:
    """在宿主整理事件边界投影候选识别所需事实。"""

    if mediainfo is None:
        return None
    aliases: list[str] = []
    for value in (
        getattr(mediainfo, "en_title", None),
        getattr(mediainfo, "original_title", None),
        *(getattr(mediainfo, "names", None) or []),
    ):
        if isinstance(value, str) and value.strip() and value.strip() not in aliases:
            aliases.append(value.strip())
    season_years = tuple(
        (str(season), str(year))
        for season, year in (getattr(mediainfo, "season_years", None) or {}).items()
        if year not in (None, "")
    )
    raw_douban_id = getattr(mediainfo, "douban_id", None)
    douban_id = str(raw_douban_id).strip() if raw_douban_id not in (None, "") else None
    raw_bangumi_id = getattr(mediainfo, "bangumi_id", None)
    raw_anilist_id = getattr(mediainfo, "anilist_id", None)
    try:
        bangumi_id = int(raw_bangumi_id) if raw_bangumi_id not in (None, "") else None
    except (TypeError, ValueError):
        bangumi_id = None
    try:
        anilist_id = int(raw_anilist_id) if raw_anilist_id not in (None, "") else None
    except (TypeError, ValueError):
        anilist_id = None
    return CandidateMatchContext(
        title=str(getattr(mediainfo, "title", None) or context.title),
        aliases=tuple(aliases),
        original_title=getattr(mediainfo, "original_title", None) or context.original_title,
        year=context.year,
        media_type=context.media_type,
        tmdb_id=context.tmdb_id,
        imdb_id=context.imdb_id,
        douban_id=douban_id,
        bangumi_id=bangumi_id,
        anilist_id=anilist_id,
        season_years=season_years,
    )


class RuntimeInitializationError(RuntimeError):
    """运行态无法完成稳定初始化时抛出的异常。"""


class PluginRuntime:
    """装配能力 facade 并统一管理插件运行期资源。"""

    def __init__(self, host: PluginHost) -> None:
        """创建绑定宿主入口的空运行态。"""

        self._host = host
        self._plugin_id = host.__class__.__name__
        self._enabled = False
        self.config = PluginConfig()
        self.store: PluginDataStore | None = None
        self.filesystem: SubtitleFiles | None = None
        self.record_committer: RecordCommitter | None = None
        self.record_catalog: RecordCatalog | None = None
        self.record_maintenance: RecordMaintenance | None = None
        self.coordinator: TaskOperations | None = None
        self.api_controller: ApiController | None = None
        self.targets: TargetCatalog | None = None
        self.manual_search: ManualSearch | None = None
        self.source_service: SourceAdministration | None = None
        self.archive: ArchiveExtractor | None = None
        self._stopped = False

    def initialize(self, config: Mapping[str, object] | None = None) -> None:
        """按宿主配置装配当前运行代次的全部能力。"""

        logger.info("字幕助手插件开始初始化或重载")
        try:
            self.config = load_config(config, settings.RMT_SUBEXT)
        except ValueError as exc:
            raise RuntimeInitializationError("插件配置无效") from exc
        store = PluginDataStore(self._host)
        try:
            store.initialize()
        except StoreInitializationError as exc:
            raise RuntimeInitializationError("插件数据初始化失败") from exc
        store.mark_nonterminal_interrupted_sync("任务在服务重启前未完成，已中断且不会自动恢复")
        store.ensure_source_statuses_sync(self.config.enabled_sources())

        filesystem = SubtitleFiles(
            data_root=self._host.get_data_path(),
            allowed_formats=settings.RMT_SUBEXT,
        )
        publisher = SubtitleEvents()
        record_committer = RecordCommitter(
            store=store,
            filesystem=filesystem,
            records=store.list_records_sync(),
            format_priority=self.config.format_priority,
            source_priority=[source.value for source in self.config.source_priority],
            publisher=publisher,
        )
        opensubtitles_credentials = store.get_credentials_sync(SubtitleSource.OPENSUBTITLES)
        assrt_credentials = store.get_credentials_sync(SubtitleSource.ASSRT)
        allowed_formats = set(settings.RMT_SUBEXT)
        source_service = SourceAdministration.build(
            moviepilot_enabled=self.config.moviepilot_enabled,
            opensubtitles_enabled=self.config.opensubtitles_enabled,
            assrt_enabled=self.config.assrt_enabled,
            opensubtitles_credentials=opensubtitles_credentials,
            assrt_credentials=assrt_credentials,
            allowed_formats=allowed_formats,
            store=store,
        )
        # AI 接管适配器只持有当前配置读取器，不保存任务结果；每个批次由适配器
        # 再次检查插件开关与 MoviePilot 总开关，避免把初始化时状态固化进任务。
        matcher = FileAttributor(config_provider=lambda: self.config)
        targets = TargetCatalog(config_provider=lambda: self.config)
        archive = ArchiveExtractor()
        coordinator = TaskOperations(
            store=store,
            filesystem=filesystem,
            archive=archive,
            matcher=matcher,
            sources=source_service,
            config=self.config,
            inventory=record_committer,
            media_extensions=settings.RMT_MEDIAEXT,
            attributor=matcher,
            candidate_pool=source_service,
            target_catalog=targets,
            manage_resources=False,
        )
        self.store = store
        self.filesystem = filesystem
        self.record_committer = record_committer
        self.coordinator = coordinator
        self.targets = targets
        self.source_service = source_service
        self.archive = archive
        self.manual_search = ManualSearch(
            targets=targets,
            candidate_pool=source_service,
            matcher=matcher,
            coordinator=coordinator,
        )
        self.record_catalog = record_committer.catalog()
        self.record_maintenance = record_committer.maintenance(targets, publisher)
        self.api_controller = ApiController(
            tasks=coordinator,
            records=self.record_catalog,
            maintenance=self.record_maintenance,
            filesystem=filesystem,
            targets=targets,
            search=self.manual_search,
            sources=source_service,
            update_credentials=self.update_source_credentials,
            clear_credentials=self.clear_source_credentials,
        )
        self._enabled = self.config.enabled
        self._stopped = False
        _PLUGIN_INSTANCES[self._plugin_id] = self
        logger.info(f"字幕助手插件初始化完成，当前{'已启用' if self._enabled else '未启用'}")

    def get_state(self) -> bool:
        """返回插件是否启用且数据初始化成功。"""

        return bool(self._enabled and self.coordinator is not None)

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """插件不提供远程命令。"""

        return []

    @staticmethod
    def get_render_mode() -> tuple[str, str]:
        """声明使用 Vue 联邦组件与构建产物目录。"""

        return "vue", "frontend/dist/assets"

    def get_sidebar_nav(self) -> list[dict[str, Any]]:
        """启用时提供整理分组中的完整工作台入口。"""

        if not self.get_state():
            return []
        return [
            {
                "nav_key": "main",
                "title": "字幕助手",
                "icon": "mdi-subtitles-outline",
                "section": "organize",
                "permission": "manage",
                "order": 30,
            }
        ]

    def get_api(self) -> list[dict[str, Any]]:
        """返回全新的 17 条 Bearer API 定义。"""

        return self.api_controller.routes() if self.api_controller else []

    def get_form(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """返回 Vue Config 的非敏感初始模型。"""

        opensubtitles_configured = bool(
            self.store and self.store.credentials_configured_sync(SubtitleSource.OPENSUBTITLES)
        )
        assrt_configured = bool(self.store and self.store.credentials_configured_sync(SubtitleSource.ASSRT))
        return [], public_config(
            self.config,
            plugin_id=self._plugin_id,
            host_ai_enabled=bool(getattr(settings, "AI_AGENT_ENABLE", False)),
            allowed_formats=[str(item).lstrip(".").upper() for item in settings.RMT_SUBEXT],
            opensubtitles_configured=opensubtitles_configured,
            assrt_configured=assrt_configured,
        )

    def get_page(self) -> None:
        """详情页由完整工作台替代。"""

    async def on_transfer_complete(self, event: Event) -> None:
        """接收运行期整理完成事件并创建或合并字幕任务。"""

        if not self.get_state() or self.coordinator is None:
            logger.debug("字幕助手当前不可用，已忽略媒体整理完成事件")
            return
        data = event.event_data if isinstance(event.event_data, dict) else {}
        transferinfo = data.get("transferinfo")
        target = getattr(transferinfo, "target_item", None)
        if target is None:
            logger.warning("媒体整理完成事件没有目标文件，无法创建自动补齐任务")
            return
        context = build_media_context(target, data.get("meta"), data.get("mediainfo"))
        if context is None:
            logger.warning("媒体整理完成事件缺少可用的媒体上下文，无法创建自动补齐任务")
            return
        logger.info(f"字幕助手收到媒体整理完成事件，目标文件为“{context.target_path}”")
        history_id = data.get("transfer_history_id")
        try:
            history_id = int(history_id) if history_id not in (None, "") else None
        except (TypeError, ValueError):
            history_id = None
        await self.coordinator.enqueue(
            TaskWorkItem(
                context=context,
                match_context=build_match_context(context, data.get("mediainfo")),
                target_history_id=history_id,
            )
        )

    async def update_source_credentials(self, source: SubtitleSource, values: dict[str, str]) -> bool:
        """增量保存外部来源凭据并返回配置完整状态。"""

        if self.store is None:
            raise RuntimeError("插件数据尚未初始化")
        source_service = getattr(self, "source_service", None)
        if source_service is not None:
            return await source_service.update_credentials(source, values)
        return await self.store.update_credentials(source, values)

    async def clear_source_credentials(self, source: SubtitleSource) -> bool:
        """删除来源凭据、立即停用来源并保存非敏感开关。"""

        if self.store is None:
            raise RuntimeError("插件数据尚未初始化")
        await self.store.clear_credentials(source)
        if source is SubtitleSource.OPENSUBTITLES:
            self.config.opensubtitles_enabled = False
        elif source is SubtitleSource.ASSRT:
            self.config.assrt_enabled = False
        source_service = getattr(self, "source_service", None)
        if source_service is not None:
            await source_service.clear_credentials(source)
        await self.store.save_source_status(
            SourceStatus(
                source=source,
                enabled=False,
                configured=False,
                health=SourceHealth.DISABLED,
            )
        )
        return bool(self._host.update_config(self.config.saved_payload(), plugin_id=self._plugin_id))

    def reset_data_sync(self) -> None:
        """同步等待插件数据目录与分区在宿主删除前清理完成。"""

        coroutine = self.reset()
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        loop = global_vars.loop
        if loop and loop.is_running() and loop is not running_loop:
            asyncio.run_coroutine_threadsafe(coroutine, loop).result()
        elif running_loop is None:
            asyncio.run(coroutine)
        else:
            # 宿主当前重置路由在线程中调用；此分支仅作防御性回退。
            running_loop.create_task(coroutine)

    async def stop(self) -> None:
        """按反向依赖顺序释放一次当前运行态资源。"""

        if self._stopped:
            return
        self._stopped = True
        self._enabled = False
        coordinator = self.coordinator
        search = self.manual_search
        source_service = self.source_service
        archive = self.archive
        for name, close in (
            ("字幕任务", coordinator.shutdown if coordinator is not None else None),
            ("人工搜索", search.clear_sessions if search is not None else None),
            ("字幕来源", source_service.close if source_service is not None else None),
            ("字幕归档", archive.cancel if archive is not None else None),
        ):
            if close is None:
                continue
            try:
                await close()
            except Exception as exc:  # noqa: BLE001 - 其他资源仍必须继续释放
                logger.error(f"字幕助手停止时释放{name}失败：{type(exc).__name__}")
        self.api_controller = None
        self.manual_search = None
        self.coordinator = None
        self.source_service = None
        self.archive = None
        self.record_catalog = None
        self.record_maintenance = None
        self.record_committer = None
        self.targets = None

    async def reset(self) -> None:
        """停止运行态并清理数据目录及 PluginData 分区。"""

        filesystem = self.filesystem
        store = self.store
        await self.stop()
        try:
            if filesystem is not None:
                await filesystem.clear_data_directory()
        except Exception as exc:  # noqa: BLE001 - PluginData 重置仍必须继续执行
            logger.error(f"字幕助手重置时清理数据目录失败：{type(exc).__name__}")
        try:
            if store is not None:
                await store.reset()
        except Exception as exc:  # noqa: BLE001 - 运行态引用仍必须释放
            logger.error(f"字幕助手重置时清理 PluginData 失败：{type(exc).__name__}")
        finally:
            self.filesystem = None
            self.store = None

    def stop_sync(self) -> None:
        """从宿主同步生命周期钩子触发幂等停止。"""

        if self._stopped:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.stop())
        else:
            loop.create_task(self.stop())


def build_runtime(host: PluginHost, config: Mapping[str, object] | None = None) -> PluginRuntime:
    """作为唯一组合根完成全部能力装配并返回运行态。"""

    runtime = PluginRuntime(host)
    try:
        runtime.initialize(config)
    except RuntimeInitializationError:
        raise
    except Exception as exc:
        raise RuntimeInitializationError("插件运行态初始化失败") from exc
    return runtime
