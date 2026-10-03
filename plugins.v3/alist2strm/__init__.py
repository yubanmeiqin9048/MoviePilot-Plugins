"""OpenList STRM 插件公共入口与宿主配置适配。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import Body, Depends, HTTPException

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


class ConfigurationPersistenceError(RuntimeError):
    """宿主配置或凭据分区写入失败。"""


class Alist2Strm(_PluginBase):
    """保存 OpenList 实例、目录映射和后续扫描所需的插件配置。"""

    plugin_name = "Alist2Strm"
    plugin_desc = "为多个 OpenList 实例生成并维护本地 STRM。"
    plugin_icon = "mdi-play-box-multiple-outline"
    plugin_version = "0.1.0"
    plugin_author = "MoviePilot"
    plugin_label = "媒体"
    plugin_config_prefix = "alist2strm_"
    plugin_order = 999
    auth_level = 2

    SECRET_DATA_KEY = "configuration_secrets_v1"
    PersistenceError = ConfigurationPersistenceError

    def __init__(self) -> None:
        """初始化宿主基类及不触发扫描的配置快照。"""

        super().__init__()
        self._plugin_instance_id = self.__class__.__name__
        self._configuration = PluginConfiguration()
        self._secret_tokens: dict[str, str] = {}
        self._configuration_valid = True
        self._stopped = False
        # 当前票据不创建扫描器；计数仅用于确保保存和启动没有隐式扫描。
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
            return
        self._configuration = candidate
        self._secret_tokens = secret_tokens(candidate)
        self._configuration_valid = True
        self._stopped = False

    def get_state(self) -> bool:
        """返回当前配置是否有效且可进入后续运行视图。"""

        return bool(getattr(self, "_configuration_valid", False) and getattr(self, "_configuration", None))

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
            "schedule_cron": configuration.schedule_cron,
            "history_retention_days": configuration.history_retention_days,
        }

    def save_configuration(self, payload: Mapping[str, object]) -> dict[str, object]:
        """先完整校验候选配置，再以可回滚顺序持久化并发布新快照。"""

        old_configuration = getattr(self, "_configuration", None)
        if not isinstance(old_configuration, PluginConfiguration):
            old_configuration = PluginConfiguration()
        old_tokens = dict(getattr(self, "_secret_tokens", {}) or {})
        candidate = build_configuration(payload, existing_tokens=old_tokens)

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
            if public_write_started and result is not False:
                self._rollback_persistence(old_public, old_tokens)
            raise
        except Exception as error:
            self._rollback_persistence(old_public, old_tokens)
            raise ConfigurationPersistenceError("配置保存失败，编辑内容未改变") from error

        self._configuration = candidate
        self._secret_tokens = new_tokens
        self._configuration_valid = True
        return {
            "success": True,
            "message": "Alist2Strm 配置已保存；当前任务如正在运行仍使用原配置。",
            "data": self.public_configuration(),
        }

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
        """返回配置读写 Bearer API，并在端点上显式要求超级管理员。"""

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
        ]

    def stop_service(self) -> None:
        """停止当前票据没有创建的后台工作，不触碰保存配置。"""

        self._stopped = True


__all__ = ["Alist2Strm", "ConfigurationPersistenceError"]
