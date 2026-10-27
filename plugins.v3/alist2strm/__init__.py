"""OpenList STRM 插件公共入口与宿主配置适配。"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from fastapi import Body, Depends, HTTPException, Query

from app.api.dependencies.auth import get_current_active_superuser_async
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .config import (
    ConfigurationValidationError,
    PluginConfiguration,
    build_configuration,
    public_configuration,
    secret_tokens,
)
from .history import (
    DEFAULT_HISTORY_PAGE_SIZE,
    MAX_HISTORY_PAGE_SIZE,
    NON_TERMINAL_STATES,
    HistoryStore,
)
from .history import HISTORY_DATA_KEY as SCAN_HISTORY_DATA_KEY
from .observations import OBSERVATIONS_DATA_KEY, DirectoryObservationStore
from .scan import ScanCoordinator, ScanTarget


class ConfigurationPersistenceError(RuntimeError):
    """宿主配置或凭据分区写入失败。"""


class Alist2Strm(_PluginBase):
    """保存 OpenList/Alist 实例、目录映射和后续扫描所需的插件配置。"""

    plugin_name = "Alist2Strm"
    plugin_desc = "为多个 OpenList/Alist 实例生成并维护本地 STRM。"
    plugin_icon = "https://raw.githubusercontent.com/yubanmeiqin9048/MoviePilot-Plugins/main/icons/Alist.png"
    plugin_version = "2.2"
    plugin_author = "yubanmeiqin9048"
    plugin_label = "网盘"
    plugin_config_prefix = "alist2strm_"
    plugin_order = 999
    auth_level = 1

    SECRET_DATA_KEY = "configuration_secrets_v1"
    HISTORY_DATA_KEY = SCAN_HISTORY_DATA_KEY
    OBSERVATIONS_DATA_KEY = OBSERVATIONS_DATA_KEY
    HISTORY_RETENTION_CRON = "0 4 * * *"
    PersistenceError = ConfigurationPersistenceError

    def __init__(self) -> None:
        """初始化宿主基类及不触发扫描的配置快照。"""

        super().__init__()
        self._plugin_instance_id = self.__class__.__name__
        self._configuration = PluginConfiguration()
        self._secret_tokens: dict[str, str] = {}
        self._configuration_valid = True
        self._stopped = False
        # 扫描由宿主一次性任务执行，周期服务仅负责检查到期工作。
        self._scan_coordinator: ScanCoordinator | None = None
        self._history_store: HistoryStore | None = None
        self._observation_store: DirectoryObservationStore | None = None
        self._history_recovery_done = False
        self._history_live_run_ids: set[str] = set()
        self._history_live_lock = threading.RLock()
        self.scan_invocations = 0

    def _plugin_id(self) -> str:
        """返回宿主当前插件实例 ID。"""

        return str(getattr(self, "_plugin_instance_id", self.__class__.__name__))

    def _read_secret_tokens(self) -> dict[str, str]:
        """读取仅服务端可见的管理员 Token 分区。"""

        try:
            value = self.get_data(self.SECRET_DATA_KEY, plugin_id=self._plugin_id())
        except Exception:  # noqa: BLE001 - 配置读取失败按未配置处理，不泄露底层信息
            return {}
        if not isinstance(value, Mapping):
            return {}
        return {
            str(key): str(token).strip()
            for key, token in value.items()
            if str(key).strip() and isinstance(token, str) and token.strip()
        }

    def _read_stored_configuration(self) -> Mapping[str, object] | None:
        """读取宿主实例公开配置。"""

        try:
            value = self.get_config(self._plugin_id())
        except Exception:  # noqa: BLE001 - 首次安装没有配置时使用默认模型
            return None
        return value if isinstance(value, Mapping) else None

    def _get_history_store(self) -> HistoryStore:
        """惰性取得 PluginData 历史边界，允许测试注入内存替身。"""

        store = getattr(self, "_history_store", None)
        if isinstance(store, HistoryStore):
            return store
        loader = getattr(self, "get_data", None)
        saver = getattr(self, "save_data", None)

        def load() -> object:
            """读取当前插件的历史载荷。"""

            if not callable(loader):
                return None
            return loader(self.HISTORY_DATA_KEY, plugin_id=self._plugin_id())

        def save(value: object) -> None:
            """保存当前插件的历史载荷。"""

            if not callable(saver):
                raise OSError("宿主 PluginData 保存能力不可用")
            saver(self.HISTORY_DATA_KEY, value, plugin_id=self._plugin_id())

        clock = getattr(self, "_history_clock", getattr(self, "_scan_clock", time.time))
        if not callable(clock):
            clock = time.time
        store = HistoryStore(
            load,
            save,
            clock=clock,
            secrets=lambda: tuple(getattr(self, "_secret_tokens", {}).values()),
        )
        self._history_store = store
        return store

    def _recover_history(self, retention_days: int) -> bool:
        """初始化时恢复旧记录；读取或写回失败时允许后续初始化重试。"""

        try:
            result = self._get_history_store().recover(
                retention_days,
                exclude_run_ids=self._history_live_snapshot(),
            )
        except Exception:  # noqa: BLE001 - 历史故障不阻止配置恢复
            logger.warning("Alist2Strm 扫描历史恢复失败，插件仍可继续运行")
            return False
        return result is not None

    def _prune_history(self, retention_days: int) -> None:
        """成功保存配置后独立清理到期历史，不依赖定时开关。"""

        try:
            self._get_history_store().prune(retention_days)
        except Exception:  # noqa: BLE001 - 清理故障不回滚已成功配置
            logger.warning("Alist2Strm 扫描历史保留策略清理失败")

    def _record_scan_history(self, status: object) -> None:
        """接收协调器状态快照并写入安全历史。"""

        run_id = self._history_status_value(status, "run_id")
        state = self._history_status_value(status, "state")
        if isinstance(run_id, str) and run_id:
            self._update_live_history(run_id, state in NON_TERMINAL_STATES)
        self._get_history_store().record_status(status)

    def _history_live_snapshot(self) -> tuple[str, ...]:
        """返回当前进程接受的运行 ID，供恢复排除同进程活动任务。"""

        lock = getattr(self, "_history_live_lock", None)
        if lock is None:
            return tuple(getattr(self, "_history_live_run_ids", set()))
        with lock:
            return tuple(self._history_live_run_ids)

    def _update_live_history(self, run_id: str, live: bool) -> None:
        """维护同一插件实例的活动历史 ID 集合。"""

        if not hasattr(self, "_history_live_run_ids"):
            self._history_live_run_ids = set()
        lock = getattr(self, "_history_live_lock", None)
        context = lock if lock is not None else nullcontext()
        with context:
            if live:
                self._history_live_run_ids.add(run_id)
            else:
                self._history_live_run_ids.discard(run_id)

    @staticmethod
    def _history_status_value(status: object, key: str) -> object:
        """读取状态对象或安全字典的公共字段。"""

        if isinstance(status, Mapping):
            return status.get(key)
        return getattr(status, key, None)

    def _recover_or_prune_history(self, retention_days: int) -> None:
        """仅在插件实例首次初始化执行重启恢复，后续重应用只清理保留记录。"""

        if not getattr(self, "_history_recovery_done", False):
            if self._recover_history(retention_days):
                self._history_recovery_done = True
        else:
            self._prune_history(retention_days)

    def _discard_scan_history(self, run_id: str) -> None:
        """移除调度拒绝的扫描初始记录。"""

        self._update_live_history(run_id, False)
        self._get_history_store().discard(run_id)

    def init_plugin(self, config: Mapping[str, object] | None = None) -> None:
        """加载并校验宿主配置，不启动扫描或私有调度器。"""

        self.scan_invocations = int(getattr(self, "scan_invocations", 0))
        raw = config if config is not None else self._read_stored_configuration()
        existing_tokens = self._read_secret_tokens()
        try:
            candidate = build_configuration(raw, existing_tokens=existing_tokens)
        except ConfigurationValidationError as error:
            self._configuration = None
            self._secret_tokens = existing_tokens
            self._configuration_valid = False
            logger.error(f"Alist2Strm 配置无效，插件未进入运行视图：{error}")
            self._recover_or_prune_history(90)
            return
        self._configuration = candidate
        self._secret_tokens = secret_tokens(candidate)
        self._configuration_valid = True
        self._stopped = False
        self._recover_or_prune_history(candidate.history_retention_days)
        self._reconcile_mapping_identities(candidate)
        logger.info(
            f"Alist2Strm 初始化完成：实例数={len(candidate.instances)}，"
            f"映射数={sum(len(instance.mappings) for instance in candidate.instances)}，"
            f"自动扫描={candidate.schedule_enabled}，活跃复查间隔={candidate.active_recheck_minutes}分钟"
        )

    def get_state(self) -> bool:
        """返回当前配置是否有效且可进入后续运行视图。"""

        return bool(getattr(self, "_configuration_valid", False) and getattr(self, "_configuration", None))

    def get_service(self) -> list[dict[str, Any]]:
        """声明历史清理服务，并按开关启用每分钟到期检查。"""

        if getattr(self, "_stopped", False):
            return []
        configuration = getattr(self, "_configuration", None)
        if not getattr(self, "_configuration_valid", False) or not isinstance(configuration, PluginConfiguration):
            return []
        common_kwargs = {
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": None,
        }
        services: list[dict[str, Any]] = [
            {
                "id": "history-retention",
                "name": "Alist2Strm 历史保留清理",
                "trigger": CronTrigger.from_crontab(self.HISTORY_RETENTION_CRON),
                "func": self.retention_cleanup,
                "kwargs": common_kwargs.copy(),
            }
        ]
        if not configuration.schedule_enabled:
            return services
        trigger = CronTrigger.from_crontab("* * * * *")
        services.insert(
            0,
            {
                "id": "shared-scan",
                "name": "Alist2Strm 到期目录检查",
                "trigger": trigger,
                "func": self.scheduled_scan,
                "kwargs": {**common_kwargs, "next_run_time": datetime.now(UTC)},
            },
        )
        return services

    def retention_cleanup(self) -> None:
        """执行仅历史 PluginData 的每日保留清理，不触发扫描。"""

        configuration = getattr(self, "_configuration", None)
        if isinstance(configuration, PluginConfiguration):
            self._prune_history(configuration.history_retention_days)

    def scheduled_scan(self) -> dict[str, object]:
        """检查到期工作后复用扫描流程，没有到期目录时不创建运行记录。"""

        configuration = getattr(self, "_configuration", None)
        if getattr(self, "_stopped", False) or configuration is None or not configuration.schedule_enabled:
            return {"accepted": False, "message": "自动扫描未启用"}
        result = self._get_scan_coordinator().trigger(only_due=True)
        if result.accepted:
            self.scan_invocations = int(getattr(self, "scan_invocations", 0)) + 1
        return result.as_dict()

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """插件不提供远程命令。"""

        return []

    @staticmethod
    def get_render_mode() -> tuple[str, str]:
        """声明 Vue 联邦模式及其构建产物目录。"""

        return "vue", "frontend/dist/assets"

    def public_configuration(self) -> dict[str, object]:
        """返回不包含任何管理员 Token 的当前配置投影。"""

        configuration = getattr(self, "_configuration", None)
        if not isinstance(configuration, PluginConfiguration):
            configuration = PluginConfiguration()
        return public_configuration(configuration, plugin_id=self._plugin_id())

    def get_form(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """返回联邦 Config 初始模型，避免宿主通用表单下发凭据。"""

        return [], self.public_configuration()

    def get_page(self) -> list[dict[str, object]]:
        """返回安全的空 Page 合约，供宿主详情弹窗加载后续页面。"""

        return []

    def _storage_payload(self, configuration: PluginConfiguration) -> dict[str, object]:
        """构造不含 Token 的宿主配置持久化载荷。"""

        instances = [
            {
                "id": instance.id,
                "name": instance.name,
                "scan_url": instance.scan_url,
                "playback_url": instance.playback_url or "",
                "mappings": [mapping.model_dump(mode="python") for mapping in instance.mappings],
            }
            for instance in configuration.instances
        ]
        return {
            "instances": instances,
            "schedule_enabled": configuration.schedule_enabled,
            "active_recheck_minutes": configuration.active_recheck_minutes,
            "history_retention_days": configuration.history_retention_days,
        }

    def save_configuration(self, payload: Mapping[str, object]) -> dict[str, object]:
        """先完整校验候选配置，再以可回滚顺序持久化并发布新快照。"""

        old_configuration = getattr(self, "_configuration", None)
        if not isinstance(old_configuration, PluginConfiguration):
            old_configuration = PluginConfiguration()
        old_tokens = dict(getattr(self, "_secret_tokens", {}) or {})
        candidate = build_configuration(payload, existing_tokens=old_tokens)
        if not self._reconcile_mapping_identities(candidate):
            raise ConfigurationPersistenceError("目录映射身份状态保存失败，编辑内容未改变")

        old_public = self._storage_payload(old_configuration)
        new_public = self._storage_payload(candidate)
        new_tokens = secret_tokens(candidate)
        public_write_started = False
        result: bool | None = None
        try:
            public_write_started = True
            result = self.update_config(new_public, plugin_id=self._plugin_id())
            if result is False:
                raise ConfigurationPersistenceError("宿主配置保存失败")
            self.save_data(self.SECRET_DATA_KEY, new_tokens, plugin_id=self._plugin_id())
        except ConfigurationPersistenceError:
            logger.error("Alist2Strm 配置保存失败：宿主拒绝写入")
            if public_write_started and result is not False:
                self._rollback_persistence(old_public, old_tokens)
            raise
        except Exception as error:
            logger.error(f"Alist2Strm 配置保存失败，开始回滚：类型={type(error).__name__}")
            self._rollback_persistence(old_public, old_tokens)
            raise ConfigurationPersistenceError("配置保存失败，编辑内容未改变") from error

        self._configuration = candidate
        self._secret_tokens = new_tokens
        self._configuration_valid = True
        self._prune_history(candidate.history_retention_days)
        self._refresh_scheduler_registration()
        logger.info(f"Alist2Strm 配置已保存：实例数={len(candidate.instances)}，自动扫描={candidate.schedule_enabled}")
        return {
            "success": True,
            "message": "Alist2Strm 配置已保存；当前任务如正在运行仍使用原配置。",
            "data": self.public_configuration(),
        }

    def _refresh_scheduler_registration(self) -> None:
        """在保存发布新快照后显式请求宿主重建本插件定时服务。"""

        injected = getattr(self, "_schedule_update", None)
        if callable(injected):
            injected(self._plugin_id())
            return
        from app.sdk.scheduler import update_plugin_job

        try:
            update_plugin_job(self._plugin_id())
        except RuntimeError as error:
            # 测试、启动早期或宿主停机时调度器可能尚未装配；配置仍已成功保存，
            # 宿主下一次生命周期对账会按新配置注册服务。
            logger.warning(f"Alist2Strm 定时服务刷新暂不可用：{error}")

    def _rollback_persistence(self, old_public: Mapping[str, object], old_tokens: Mapping[str, str]) -> None:
        """尽力恢复保存失败前的公开配置和 Token 分区，不记录敏感值。"""

        try:
            self.update_config(dict(old_public), plugin_id=self._plugin_id())
        except Exception:  # noqa: BLE001 - 原始失败优先返回，回滚异常不覆盖它
            logger.error("Alist2Strm 配置保存失败后的公开配置回滚失败")
        try:
            self.save_data(self.SECRET_DATA_KEY, dict(old_tokens), plugin_id=self._plugin_id())
        except Exception:  # noqa: BLE001 - 原始失败优先返回，回滚异常不覆盖它
            logger.error("Alist2Strm 配置保存失败后的凭据分区回滚失败")

    def _configuration_for_scan(self) -> PluginConfiguration | None:
        """返回当前已保存且有效的配置，供扫描入口在启动时复制。"""

        if getattr(self, "_stopped", False):
            return None
        configuration = getattr(self, "_configuration", None)
        if not getattr(self, "_configuration_valid", False) or not isinstance(configuration, PluginConfiguration):
            return None
        return configuration

    def _dispatch_scan(self, task: Any) -> bool:
        """把扫描提交给注入的测试调度器或宿主一次性任务 SDK。"""

        injected = getattr(self, "_scan_dispatch", None)
        if callable(injected):
            return bool(injected(task))
        from app.sdk.scheduler import add_plugin_once_job

        return bool(
            add_plugin_once_job(
                self._plugin_id(),
                "manual-scan",
                self._execute_dispatched_scan,
                "Alist2Strm 手动扫描",
                func_kwargs={"task": task},
            )
        )

    def _execute_dispatched_scan(self, task: Any) -> None:
        """在插件实例绑定的一次性任务中执行已捕获的扫描回调。"""

        task()

    def _reconcile_mapping_identities(self, configuration: PluginConfiguration) -> bool:
        """在配置发布前持久化身份过渡，初始化失败由下一次任务构建重试。"""

        return self._get_observation_store().reconcile_mappings(
            (instance.id, mapping.source_dir, mapping.output_dir)
            for instance in configuration.instances
            for mapping in instance.mappings
        )

    def _get_observation_store(self) -> DirectoryObservationStore:
        """惰性绑定当前插件实例的 PluginData 观测分区，不触发扫描。"""

        store = getattr(self, "_observation_store", None)
        if not isinstance(store, DirectoryObservationStore):
            store = DirectoryObservationStore(
                lambda: self.get_data(self.OBSERVATIONS_DATA_KEY, plugin_id=self._plugin_id()),
                lambda value: self.save_data(self.OBSERVATIONS_DATA_KEY, value, plugin_id=self._plugin_id()),
            )
            self._observation_store = store
        return store

    def _get_scan_coordinator(self) -> ScanCoordinator:
        """惰性取得全插件唯一扫描协调器，兼容无构造函数的 seam 测试对象。"""

        coordinator = getattr(self, "_scan_coordinator", None)
        if not isinstance(coordinator, ScanCoordinator):
            coordinator = ScanCoordinator(
                self._configuration_for_scan,
                plugin_id=self._plugin_id(),
                dispatch=self._dispatch_scan,
                client_factory=getattr(self, "_scan_client_factory", None),
                media_suffix_provider=getattr(self, "_scan_media_suffix_provider", None),
                whitelist_suffix_provider=getattr(self, "_scan_whitelist_suffix_provider", None),
                filesystem=getattr(self, "_scan_filesystem", None),
                clock=getattr(self, "_scan_clock", time.time),
                sleep=getattr(self, "_scan_sleep", None),
                retry_backoff=getattr(self, "_scan_retry_backoff", None),
                max_retries=getattr(self, "_scan_max_retries", 2),
                history_callback=self._record_scan_history,
                history_discard=self._discard_scan_history,
                observation_store=self._get_observation_store(),
            )
            self._scan_coordinator = coordinator
        return coordinator

    def trigger_scan(self, target: ScanTarget | None = None, *, force_complete: bool = False) -> dict[str, object]:
        """捕获已保存配置并提交普通扫描或强制完整扫描。"""

        result = self._get_scan_coordinator().trigger(target, force_complete=force_complete)
        if result.accepted:
            self.scan_invocations = int(getattr(self, "scan_invocations", 0)) + 1
        return result.as_dict()

    def get_scan_status(self) -> dict[str, object]:
        """返回当前或最近一次扫描的生命周期、映射和六类计数。"""

        return self._get_scan_coordinator().status().as_dict()

    @staticmethod
    def _scan_target(payload: object, *, required: bool = False) -> ScanTarget | None:
        """解析运行接口目标，不接收或保存浏览器配置草稿。"""

        value: object = payload
        if isinstance(value, Mapping):
            value = (
                value.get("target")
                if "target" in value
                else {key: item for key, item in value.items() if key != "scan_mode"}
            )
        if value is None or (isinstance(value, Mapping) and not value and not required):
            if required:
                raise HTTPException(status_code=422, detail="必须指定目录映射")
            return None
        if not isinstance(value, Mapping):
            raise HTTPException(status_code=422, detail="目录映射参数无效")
        instance_id = value.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise HTTPException(status_code=422, detail="实例 ID 无效")
        mapping_index = value.get("mapping_index")
        if mapping_index is not None and (
            isinstance(mapping_index, bool) or not isinstance(mapping_index, int) or mapping_index < 1
        ):
            raise HTTPException(status_code=422, detail="映射序号无效")
        source_dir = value.get("source_dir")
        if source_dir is not None and not isinstance(source_dir, str):
            raise HTTPException(status_code=422, detail="源目录无效")
        if mapping_index is None and not isinstance(source_dir, str):
            raise HTTPException(status_code=422, detail="必须指定映射序号或源目录")
        return ScanTarget(
            instance_id=instance_id.strip(),
            mapping_index=mapping_index,
            source_dir=source_dir,
        )

    @staticmethod
    def _force_complete(payload: object) -> bool:
        """仅接受已定义的扫描方式，缺省仍为普通运行。"""

        mode = payload.get("scan_mode", "normal") if isinstance(payload, Mapping) else "normal"
        if mode not in ("normal", "force_complete"):
            raise HTTPException(status_code=422, detail="扫描方式无效")
        return mode == "force_complete"

    def start_scan(
        self,
        payload: dict[str, Any] | None = Body(None),  # noqa: B008 - FastAPI 请求体注入
        _: Any = Depends(get_current_active_superuser_async),  # noqa: B008 - FastAPI 权限注入
    ) -> dict[str, object]:
        """运行全部映射，兼容带 target 的单组请求并使用启动快照。"""

        force_complete = self._force_complete(payload)
        target = self._scan_target(payload)
        return self.trigger_scan(target, force_complete=True) if force_complete else self.trigger_scan(target)

    def run_mapping_scan(
        self,
        payload: dict[str, Any] | None = Body(None),  # noqa: B008 - FastAPI 请求体注入
        _: Any = Depends(get_current_active_superuser_async),  # noqa: B008 - FastAPI 权限注入
    ) -> dict[str, object]:
        """运行一个已保存目录映射，不把配置草稿传入扫描任务。"""

        force_complete = self._force_complete(payload)
        target = self._scan_target(payload, required=True)
        return self.trigger_scan(target, force_complete=True) if force_complete else self.trigger_scan(target)

    def scan_status(
        self,
        _: Any = Depends(get_current_active_superuser_async),  # noqa: B008 - FastAPI 权限注入
    ) -> dict[str, object]:
        """读取当前扫描状态，供详情弹窗轮询。"""

        return self.get_scan_status()

    def get_history_page(self, page: int = 1, page_size: int = DEFAULT_HISTORY_PAGE_SIZE) -> dict[str, object]:
        """返回安全、倒序分页的扫描历史，并在访问时执行保留清理。"""

        configuration = getattr(self, "_configuration", None)
        retention_days = getattr(configuration, "history_retention_days", 90)
        return self._get_history_store().page(page, page_size, retention_days=retention_days)

    def read_history(
        self,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=DEFAULT_HISTORY_PAGE_SIZE, ge=1, le=MAX_HISTORY_PAGE_SIZE),
        _: Any = Depends(get_current_active_superuser_async),  # noqa: B008 - FastAPI 权限注入
    ) -> dict[str, object]:
        """分页读取管理员扫描历史，不下发触发、清理或逐文件信息。"""

        return self.get_history_page(page, page_size)

    def stop_scan(
        self,
        _: Any = Depends(get_current_active_superuser_async),  # noqa: B008 - FastAPI 权限注入
    ) -> dict[str, object]:
        """请求停止当前扫描，实际结束前状态保持正在停止。"""

        return self._get_scan_coordinator().stop().as_dict()

    def read_config(self, _: Any = Depends(get_current_active_superuser_async)) -> dict[str, object]:  # noqa: B008 - FastAPI 权限注入
        """返回已保存的安全配置投影。"""

        return {"success": True, "data": self.public_configuration()}

    def save_config(
        self,
        payload: dict[str, Any] = Body(...),  # noqa: B008 - FastAPI 请求体注入
        _: Any = Depends(get_current_active_superuser_async),  # noqa: B008 - FastAPI 权限注入
    ) -> dict[str, object]:
        """校验并保存配置，失败时保留服务端旧快照。"""

        try:
            return self.save_configuration(payload)
        except ConfigurationValidationError as error:
            raise HTTPException(status_code=422, detail=error.as_detail()) from error
        except ConfigurationPersistenceError as error:
            raise HTTPException(
                status_code=500,
                detail={"code": "configuration_persistence_failed", "message": str(error)},
            ) from error

    def get_api(self) -> list[dict[str, Any]]:
        """返回配置与扫描 Bearer API，并在端点上显式要求超级管理员。"""

        return [
            {
                "path": "/config",
                "endpoint": self.read_config,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "读取 Alist2Strm 配置",
            },
            {
                "path": "/config",
                "endpoint": self.save_config,
                "methods": ["PUT"],
                "auth": "bear",
                "summary": "保存 Alist2Strm 配置",
            },
            {
                "path": "/scan",
                "endpoint": self.start_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "运行全部或指定映射",
            },
            {
                "path": "/scan/mapping",
                "endpoint": self.run_mapping_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "运行单组映射",
            },
            {
                "path": "/scan/status",
                "endpoint": self.scan_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "读取扫描状态",
            },
            {
                "path": "/history",
                "endpoint": self.read_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "分页读取扫描历史",
            },
            {
                "path": "/scan/stop",
                "endpoint": self.stop_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "停止当前扫描",
            },
        ]

    def stop_service(self) -> None:
        """停止接收扫描、移除宿主任务、请求活动扫描停止并释放资源。"""

        self._stopped = True
        try:
            from app.sdk.scheduler import remove_plugin_job

            remove_plugin_job(self._plugin_id())
        except Exception:  # noqa: BLE001 - 卸载时调度器可能已经关闭
            logger.warning("Alist2Strm 周期调度任务取消失败")
        try:
            from app.sdk.scheduler import remove_plugin_once_job

            remove_plugin_once_job(self._plugin_id(), "manual-scan")
        except Exception:  # noqa: BLE001 - 卸载时调度器可能已经关闭
            logger.warning("Alist2Strm 一次性扫描任务取消失败")
        coordinator = getattr(self, "_scan_coordinator", None)
        if isinstance(coordinator, ScanCoordinator):
            coordinator.shutdown()


__all__ = ["Alist2Strm", "ConfigurationPersistenceError"]
