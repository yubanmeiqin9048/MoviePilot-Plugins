"""MoviePilot 字幕助手插件入口。"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

from app.core.config import global_vars, settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, EventType

from .api.router import ApiController
from .application.config import PluginConfig
from .application.inventory import SubtitleInventory
from .application.ports import SubtitleSourcePort
from .application.record_lock import ReentrantAsyncLock
from .application.retargeting import RetargetService
from .application.searches import ManualSearchService, TargetQueryService
from .application.tasks import TaskCoordinator, TaskWorkItem, build_media_context
from .domain.enums import SourceHealth, SubtitleSource
from .domain.models import SourceStatus
from .infrastructure.ai_attribution import AiAttributionAdapter
from .infrastructure.archive import ArchiveExtractor
from .infrastructure.filesystem import SubtitleFileSystem
from .infrastructure.matching import MoviePilotMatcher
from .infrastructure.store import PluginStore, StoreInitializationError
from .sources.assrt import AssrtSource
from .sources.moviepilot import MoviePilotSource
from .sources.opensubtitles import OpenSubtitlesSource

_PLUGIN_INSTANCES: weakref.WeakValueDictionary[str, SubtitleAssistant] = weakref.WeakValueDictionary()


@eventmanager.register(ChainEventType.PluginDataReset)
def _handle_plugin_data_reset(event: Event) -> None:
    """在宿主删除 PluginData 前同步清理当前插件自有文件。"""

    payload = event.event_data
    plugin_id = getattr(payload, "plugin_id", None)
    if not plugin_id or not bool(getattr(payload, "reset_data", False)):
        return
    plugin = _PLUGIN_INSTANCES.get(str(plugin_id))
    if plugin is not None:
        plugin.reset_data_sync()


class SubtitleAssistant(_PluginBase):
    """提供字幕搜索、归属、落盘、维护与审计的全生命周期管理。"""

    plugin_name = "字幕助手"
    plugin_desc = "覆盖搜索、匹配、下载、归属、落盘、维护与审计的字幕全生命周期管理。"
    plugin_icon = (
        "https://raw.githubusercontent.com/yubanmeiqin9048/MoviePilot-Plugins/main/icons/SubtitleAssistant.png"
    )
    plugin_version = "1.1"
    plugin_author = "yubanmeiqin9048"
    plugin_label = "字幕"
    plugin_config_prefix = "subtitleassistant_"
    plugin_order = 30
    auth_level = 1

    def __init__(self) -> None:
        """初始化插件基类与空运行态。"""

        super().__init__()
        self._enabled = False
        self.config = PluginConfig()
        self.store: PluginStore | None = None
        self.filesystem: SubtitleFileSystem | None = None
        self.inventory: SubtitleInventory | None = None
        self.coordinator: TaskCoordinator | None = None
        self.api_controller: ApiController | None = None
        self.targets: TargetQueryService | None = None
        self.manual_search: ManualSearchService | None = None
        self.retargeting: RetargetService | None = None
        self.record_mutation_lock: ReentrantAsyncLock | None = None

    def init_plugin(self, config: dict | None = None) -> None:
        """停止旧运行代次并按新配置完整重建插件服务。"""

        logger.info("字幕助手插件开始初始化或重载")
        self.stop_service()
        self._enabled = False
        try:
            self.config = PluginConfig.from_mapping(config, settings.RMT_SUBEXT)
        except ValueError as exc:
            self.config = PluginConfig()
            logger.error(f"字幕助手插件配置无效，未启动服务：{exc}")
            self.store = None
            self.api_controller = None
            return
        store = PluginStore(self)
        try:
            store.initialize()
        except StoreInitializationError as exc:
            logger.error(f"字幕助手插件数据初始化失败：{type(exc).__name__}")
            self.store = None
            self.api_controller = None
            return
        store.mark_nonterminal_interrupted_sync("任务在服务重启前未完成，已中断且不会自动恢复")
        store.ensure_source_statuses_sync(self.config.enabled_sources())

        filesystem = SubtitleFileSystem(
            data_root=self.get_data_path(),
            allowed_formats=settings.RMT_SUBEXT,
        )
        record_mutation_lock = ReentrantAsyncLock()
        inventory = SubtitleInventory(
            store=store,
            filesystem=filesystem,
            records=store.list_records_sync(),
            format_priority=self.config.format_priority,
            source_priority=self.config.source_priority,
            mutation_lock=record_mutation_lock,
        )
        opensubtitles_credentials = store.get_credentials_sync(SubtitleSource.OPENSUBTITLES)
        assrt_credentials = store.get_credentials_sync(SubtitleSource.ASSRT)
        allowed_formats = set(settings.RMT_SUBEXT)
        sources: dict[SubtitleSource, SubtitleSourcePort] = {
            SubtitleSource.MOVIEPILOT: MoviePilotSource(
                enabled=self.config.moviepilot_enabled,
                allowed_formats=allowed_formats,
            ),
            SubtitleSource.OPENSUBTITLES: OpenSubtitlesSource(
                enabled=self.config.opensubtitles_enabled,
                credentials=opensubtitles_credentials,
                allowed_formats=allowed_formats,
            ),
            SubtitleSource.ASSRT: AssrtSource(
                enabled=self.config.assrt_enabled,
                credentials=assrt_credentials,
                allowed_formats=allowed_formats,
            ),
        }
        # AI 接管适配器只持有当前配置读取器，不保存任务结果；每个批次由适配器
        # 再次检查插件开关与 MoviePilot 总开关，避免把初始化时状态固化进任务。
        ai_adapter = AiAttributionAdapter(config=lambda: self.config)
        matcher = MoviePilotMatcher()
        coordinator = TaskCoordinator(
            store=store,
            filesystem=filesystem,
            archive=ArchiveExtractor(),
            matcher=matcher,
            sources=sources,
            config=self.config,
            inventory=inventory,
            ai_adapter=ai_adapter,
        )
        targets = TargetQueryService()
        self.store = store
        self.filesystem = filesystem
        self.inventory = inventory
        self.coordinator = coordinator
        self.targets = targets
        self.manual_search = ManualSearchService(targets=targets, sources=sources, matcher=matcher)
        self.retargeting = RetargetService(
            store=store,
            filesystem=filesystem,
            inventory=inventory,
            targets=targets,
            config_provider=lambda: self.config,
            mutation_lock=record_mutation_lock,
        )
        self.record_mutation_lock = record_mutation_lock
        self.api_controller = ApiController(self)
        self._enabled = self.config.enabled
        _PLUGIN_INSTANCES[self.__class__.__name__] = self
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
        return [], self.config.public_payload(
            plugin_id=self.__class__.__name__,
            allowed_formats=settings.RMT_SUBEXT,
            opensubtitles_configured=opensubtitles_configured,
            assrt_configured=assrt_configured,
            host_ai_enabled=bool(getattr(settings, "AI_AGENT_ENABLE", False)),
        )

    def get_page(self) -> None:
        """详情页由完整工作台替代。"""

    @eventmanager.register(EventType.TransferComplete)
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
                target=target,
                host_mediainfo=data.get("mediainfo"),
                target_history_id=history_id,
            )
        )

    async def update_source_credentials(self, source: SubtitleSource, values: dict[str, str]) -> bool:
        """增量保存外部来源凭据并返回配置完整状态。"""

        if self.store is None:
            raise RuntimeError("插件数据尚未初始化")
        configured = await self.store.update_credentials(source, values)
        credentials = await self.store.get_credentials(source)
        if self.coordinator and source in self.coordinator.sources:
            adapter = self.coordinator.sources[source]
            if isinstance(adapter, (OpenSubtitlesSource, AssrtSource)):
                await adapter.replace_credentials(credentials)
        return configured

    async def clear_source_credentials(self, source: SubtitleSource) -> bool:
        """删除来源凭据、立即停用来源并保存非敏感开关。"""

        if self.store is None:
            raise RuntimeError("插件数据尚未初始化")
        await self.store.clear_credentials(source)
        if source is SubtitleSource.OPENSUBTITLES:
            self.config.opensubtitles_enabled = False
        elif source is SubtitleSource.ASSRT:
            self.config.assrt_enabled = False
        if self.coordinator and source in self.coordinator.sources:
            adapter = self.coordinator.sources[source]
            adapter.enabled = False
            if isinstance(adapter, (OpenSubtitlesSource, AssrtSource)):
                await adapter.replace_credentials({})
        await self.store.save_source_status(
            SourceStatus(
                source=source,
                enabled=False,
                configured=False,
                health=SourceHealth.DISABLED,
            )
        )
        return bool(self.update_config(self.config.saved_payload(), plugin_id=self.__class__.__name__))

    def reset_data_sync(self) -> None:
        """同步等待插件数据目录与分区在宿主删除前清理完成。"""

        if self.coordinator is None:
            return
        coroutine = self.coordinator.reset()
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

    def stop_service(self) -> None:
        """立即中断当前及等待任务并释放运行态资源。"""

        if self._enabled or self.manual_search is not None or self.coordinator is not None:
            logger.info("字幕助手插件正在停止服务")
        self._enabled = False
        if self.manual_search is not None:
            coroutine = self.manual_search.clear_sessions()
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is None:
                asyncio.run(coroutine)
            else:
                running_loop.create_task(coroutine)
        if self.coordinator is not None:
            self.coordinator.stop_sync()
