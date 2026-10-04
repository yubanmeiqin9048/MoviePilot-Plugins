"""Alist2Strm 配置模型、公开投影与保存前校验。"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

ADMIN_TOKEN_WARNING = "仅支持 OpenList 管理员 API Token，不支持普通用户 Token、账号或密码。"
ROOT_WARNING = "OpenList 管理员根目录必须为 /；插件不探测或换算用户根目录。"
SIGN_WARNING = "请关闭 OpenList 的 sign；插件不探测、生成或续签 sign。"
OUTPUT_DIRECTORY_WARNING = "完整扫描成功后，会清理该目录中不再需要的受管文件，包括手工放入的文件；请使用专用输出目录。"
DEFAULT_SCHEDULE_CRON = "0 3 * * *"
DEFAULT_HISTORY_RETENTION_DAYS = 90

WhitelistMode = Literal["follow_host", "custom"]


def _alias_fields(value: object, aliases: Mapping[str, Sequence[str]]) -> object:
    """把兼容输入字段收敛到一个公开配置拼写。"""

    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    for canonical, names in aliases.items():
        if canonical not in result:
            for name in names:
                if name in result:
                    result[canonical] = result[name]
                    break
        for name in names:
            result.pop(name, None)
    return result


def _normalise_absolute_path(value: str) -> str:
    """规范化一个 OpenList 或本地输出绝对路径。"""

    path = value.strip()
    if not path:
        raise ValueError("目录不能为空")
    if "\x00" in path:
        raise ValueError("目录不能包含 NUL 字符")
    if not path.startswith("/"):
        raise ValueError("源目录和输出目录必须是绝对路径")
    normalized = posixpath.normpath(path)
    return "/" if normalized in {"", "//"} else normalized


def _normalise_url(value: str, *, field_name: str) -> str:
    """校验并规范 HTTP(S) 服务地址。"""

    url = value.strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name}必须是完整的 HTTP(S) 地址")
    if any(character.isspace() for character in url):
        raise ValueError(f"{field_name}不能包含空白字符")
    if parsed.username or parsed.password:
        raise ValueError(f"{field_name}不能包含凭据")
    return url.rstrip("/") or url


def _normalise_extension(value: object) -> str:
    """把一个后缀规范成带点的小写形式。"""

    extension = str(value).strip().lower()
    if not extension:
        raise ValueError("后缀不能为空")
    if not extension.startswith("."):
        extension = f".{extension}"
    if (
        extension in {".", ".."}
        or "/" in extension
        or "\\" in extension
        or any(character in extension for character in "*?\x00")
    ):
        raise ValueError(f"后缀无效：{extension}")
    return extension


def _normalise_keywords(value: object) -> list[str]:
    """规范每行一个的排除关键字。"""

    if isinstance(value, str):
        values: Sequence[object] = value.splitlines()
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    elif value is None:
        return []
    else:
        raise ValueError("排除关键字必须是字符串列表")
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            raise TypeError("排除关键字必须是字符串")
        keyword = item.strip()
        if not keyword or keyword.casefold() in seen:
            continue
        seen.add(keyword.casefold())
        result.append(keyword)
    return result


def _is_path_overlap(first: str, second: str) -> bool:
    """返回两个输出路径按词法或符号链接解析后是否相等或互为父子。"""

    first_path = PurePosixPath(first)
    second_path = PurePosixPath(second)
    if first_path == second_path or first_path in second_path.parents or second_path in first_path.parents:
        return True
    try:
        resolved_first = Path(first).resolve(strict=False)
        resolved_second = Path(second).resolve(strict=False)
    except (OSError, RuntimeError):  # fmt: skip
        # 无法确认输出边界时拒绝保存，避免把清理范围交给不确定的路径。
        return True
    return (
        resolved_first == resolved_second
        or resolved_first in resolved_second.parents
        or resolved_second in resolved_first.parents
    )


def _valid_cron(value: str) -> str:
    """校验五字段 Cron 表达式，不创建或更新调度任务。"""

    cron = value.strip()
    if len(cron.split()) != 5:
        raise ValueError("Cron 必须包含 5 个字段")
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(cron)
    except ImportError:
        # 纯 schema 使用时允许没有宿主调度依赖，但仍保证基本字段数。
        pass
    except ValueError as error:
        raise ValueError(f"Cron 表达式无效：{error}") from error
    return cron


class DirectoryMapping(BaseModel):
    """一个 OpenList 源目录到本地输出目录的映射。"""

    model_config = ConfigDict(extra="forbid")

    source_dir: str
    output_dir: str
    whitelist_mode: WhitelistMode = "follow_host"
    extensions: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalise_input(cls, value: object) -> object:
        """接收历史字段名并去掉仅用于展示的字段。"""

        result = dict(value) if isinstance(value, Mapping) else value
        if isinstance(result, dict):
            # 这些字段只存在于公开 Vue 投影，不能回流为持久化模型的未知字段。
            result.pop("effective_extensions", None)
            result.pop("output_directory_warning", None)
        return _alias_fields(
            result,
            {
                "source_dir": ("source", "source_path", "source_prefix"),
                "output_dir": ("output", "output_path", "target_prefix", "target_dir"),
                "extensions": ("whitelist", "include_extensions", "allowed_extensions"),
                "whitelist_mode": ("mode",),
            },
        )

    @field_validator("source_dir", "output_dir")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        """拒绝相对路径、通配符和未规范化的危险输入。"""

        if "*" in value or "?" in value:
            raise ValueError("目录不能包含通配符")
        return _normalise_absolute_path(value)

    @field_validator("whitelist_mode", mode="before")
    @classmethod
    def _normalise_mode(cls, value: object) -> str:
        """兼容 UI 的默认模式名称。"""

        normalized = str(value or "follow_host").strip().lower()
        if normalized in {"default", "follow", "host", "follow-host"}:
            return "follow_host"
        return normalized

    @field_validator("extensions", mode="before")
    @classmethod
    def _normalise_extensions(cls, value: object) -> list[str]:
        """规范自定义白名单后缀。"""

        if value is None:
            return []
        if isinstance(value, str):
            values: Sequence[object] = value.splitlines()
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            values = value
        else:
            raise TypeError("后缀白名单必须是字符串列表")
        result: list[str] = []
        for item in values:
            extension = _normalise_extension(item)
            if extension not in result:
                result.append(extension)
        return result

    @field_validator("exclude_keywords", mode="before")
    @classmethod
    def _normalise_exclude_keywords(cls, value: object) -> list[str]:
        """规范排除关键字。"""

        return _normalise_keywords(value)


class OpenListInstance(BaseModel):
    """一个 OpenList 实例及其目录映射。"""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    name: str
    scan_url: str
    api_token: str = ""
    playback_url: str | None = None
    mappings: list[DirectoryMapping] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalise_input(cls, value: object) -> object:
        """接收前端和历史配置使用的连接字段别名。"""

        result = dict(value) if isinstance(value, Mapping) else value
        if isinstance(result, dict):
            # Token 永远不从浏览器的“已配置”标志反推真实值。
            result.pop("api_token_configured", None)
            result.pop("token_configured", None)
        return _alias_fields(
            result,
            {
                "id": ("instance_id",),
                "scan_url": ("scan_address", "scan_endpoint", "url", "address"),
                "api_token": ("admin_api_token", "token", "admin_token"),
                "playback_url": ("play_url", "player_url"),
                "mappings": ("directory_mappings", "path_mappings"),
            },
        )

    @field_validator("id", "name", "api_token")
    @classmethod
    def _trim_text(cls, value: str) -> str:
        """去掉身份、名称和凭据外围空白。"""

        return value.strip()

    @field_validator("name")
    @classmethod
    def _non_empty_name(cls, value: str) -> str:
        """要求实例名称可用于冲突提示。"""

        if not value:
            raise ValueError("实例名称不能为空")
        return value

    @field_validator("scan_url")
    @classmethod
    def _scan_address(cls, value: str) -> str:
        """校验扫描地址。"""

        return _normalise_url(value, field_name="扫描地址")

    @field_validator("playback_url", mode="before")
    @classmethod
    def _playback_address(cls, value: object) -> str | None:
        """允许播放地址为空并在运行时回退扫描地址。"""

        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            raise TypeError("播放地址必须是字符串")
        return _normalise_url(value, field_name="播放地址")

    @property
    def effective_playback_url(self) -> str:
        """返回当前映射应使用的播放地址。"""

        return self.playback_url or self.scan_url


class PluginConfiguration(BaseModel):
    """Alist2Strm 的可持久化公开配置。"""

    model_config = ConfigDict(extra="forbid")

    instances: list[OpenListInstance] = Field(default_factory=list)
    schedule_enabled: StrictBool = False
    schedule_cron: str = DEFAULT_SCHEDULE_CRON
    history_retention_days: StrictInt = Field(default=DEFAULT_HISTORY_RETENTION_DAYS, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _normalise_input(cls, value: object) -> object:
        """去掉只属于浏览器投影的字段。"""

        return _alias_fields(
            value,
            {
                "instances": ("openlist_instances",),
                "schedule_enabled": ("enable_schedule",),
                "schedule_cron": ("cron",),
                "history_retention_days": ("retention_days",),
            },
        )

    @field_validator("schedule_cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        """校验并规范共用定时计划。"""

        return _valid_cron(value)

    @model_validator(mode="after")
    def _enabled_schedule_has_cron(self) -> PluginConfiguration:
        """启用定时计划时不允许空 Cron。"""

        if self.schedule_enabled and not self.schedule_cron.strip():
            raise ValueError("启用定时扫描时必须填写 Cron")
        return self


class ConfigurationValidationError(ValueError):
    """保存前配置校验失败。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_configuration",
        errors: list[dict[str, object]] | None = None,
        conflicts: list[dict[str, object]] | None = None,
    ) -> None:
        """保存结构化错误信息，供 API 和界面共同展示。"""

        super().__init__(message)
        self.code = code
        self.errors = errors or []
        self.conflicts = conflicts or []

    def as_detail(self) -> dict[str, object]:
        """转换为安全的 HTTP 错误详情。"""

        detail: dict[str, object] = {"code": self.code, "message": str(self)}
        if self.errors:
            detail["errors"] = self.errors
        if self.conflicts:
            detail["conflicts"] = self.conflicts
        return detail


def _pydantic_error_detail(error: ValidationError) -> list[dict[str, object]]:
    """把 Pydantic 错误投影为前端可读且不含秘密的字段信息。"""

    details: list[dict[str, object]] = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "配置"
        details.append({"field": location, "message": str(item.get("msg", "配置无效"))})
    return details


def _raw_instances(payload: Mapping[str, object]) -> list[object]:
    """读取实例列表，避免在保存失败时改变调用方对象。"""

    values = payload.get("instances", [])
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    return list(values)


def find_output_conflicts(configuration: PluginConfiguration) -> list[dict[str, object]]:
    """返回所有相等或互为父子输出目录冲突。"""

    flattened: list[tuple[str, int, DirectoryMapping]] = []
    for instance in configuration.instances:
        for mapping_index, mapping in enumerate(instance.mappings, start=1):
            flattened.append((instance.name, mapping_index, mapping))
    conflicts: list[dict[str, object]] = []
    for index, (first_name, first_mapping_index, first) in enumerate(flattened):
        for second_name, second_mapping_index, second in flattened[index + 1 :]:
            if not _is_path_overlap(first.output_dir, second.output_dir):
                continue
            conflicts.append(
                {
                    "first_instance": first_name,
                    "first_mapping": first_mapping_index,
                    "first_output_dir": first.output_dir,
                    "second_instance": second_name,
                    "second_mapping": second_mapping_index,
                    "second_output_dir": second.output_dir,
                    "message": (
                        f"实例“{first_name}”映射 {first_mapping_index} 的输出目录 “{first.output_dir}” "
                        f"与实例“{second_name}”映射 {second_mapping_index} 的输出目录 “{second.output_dir}” 重叠"
                    ),
                }
            )
    return conflicts


def build_configuration(
    payload: Mapping[str, object] | PluginConfiguration | None,
    *,
    existing_tokens: Mapping[str, str] | None = None,
) -> PluginConfiguration:
    """完整校验并构造候选配置，失败时不触碰持久化或运行态。"""

    if payload is None:
        source: Mapping[str, object] = {}
    elif isinstance(payload, PluginConfiguration):
        source = payload.model_dump(mode="python")
    elif isinstance(payload, Mapping):
        source = dict(payload)
    else:
        raise ConfigurationValidationError("配置必须是 JSON 对象")
    # 宿主表单会把公开投影字段原样回传；这些字段不属于可持久化契约。
    source.pop("plugin_id", None)
    source.pop("warnings", None)
    source.pop("notices", None)
    source.pop("allowed_extensions", None)
    raw_instances = _raw_instances(source)
    try:
        configuration = PluginConfiguration.model_validate(source)
    except ValidationError as error:
        details = _pydantic_error_detail(error)
        message = details[0]["message"] if details else "配置校验失败"
        raise ConfigurationValidationError(str(message), errors=details) from error

    existing = {str(key): str(value).strip() for key, value in (existing_tokens or {}).items() if str(value).strip()}
    tokens: dict[str, str] = {}
    names: set[str] = set()
    normalized_instances: list[OpenListInstance] = []
    for index, instance in enumerate(configuration.instances):
        instance_id = instance.id or f"instance-{index + 1}"
        if instance_id in {item.id for item in normalized_instances}:
            raise ConfigurationValidationError(f"实例 ID 重复：{instance_id}")
        name_key = instance.name.casefold()
        if name_key in names:
            raise ConfigurationValidationError(f"实例名称重复：{instance.name}")
        names.add(name_key)
        raw_instance = raw_instances[index] if index < len(raw_instances) else {}
        raw_token = ""
        if isinstance(raw_instance, Mapping):
            for token_key in ("api_token", "admin_api_token", "token", "admin_token"):
                value = raw_instance.get(token_key)
                if isinstance(value, str) and value.strip():
                    raw_token = value.strip()
                    break
        token = raw_token or instance.api_token or existing.get(instance_id, "")
        if not token:
            raise ConfigurationValidationError(f"实例“{instance.name}”必须填写管理员 API Token")
        normalized = instance.model_copy(update={"id": instance_id, "api_token": token})
        normalized_instances.append(normalized)
        tokens[instance_id] = token

    normalized_configuration = configuration.model_copy(update={"instances": normalized_instances})
    conflicts = find_output_conflicts(normalized_configuration)
    if conflicts:
        messages = "；".join(str(item["message"]) for item in conflicts)
        raise ConfigurationValidationError(
            f"输出目录不能重复或互相包含：{messages}",
            code="output_directory_conflict",
            conflicts=conflicts,
        )
    # 属性挂在模型外部会丢失，保存 token map 供 Alist2Strm 使用；不进入 public_configuration。
    object.__setattr__(normalized_configuration, "_secret_tokens", tokens)
    return normalized_configuration


def secret_tokens(configuration: PluginConfiguration) -> dict[str, str]:
    """返回仅供服务端保存的实例 Token 映射。"""

    values = getattr(configuration, "_secret_tokens", None)
    if isinstance(values, Mapping):
        return {str(key): str(value) for key, value in values.items()}
    return {
        instance.id: instance.api_token for instance in configuration.instances if instance.id and instance.api_token
    }


def host_default_extensions() -> list[str]:
    """读取宿主当前媒体与字幕后缀，供 follow_host 模式动态投影。"""

    try:
        from app.sdk.config import settings

        values = [*getattr(settings, "RMT_MEDIAEXT", ()), *getattr(settings, "RMT_SUBEXT", ())]
    except (ImportError, AttributeError):  # fmt: skip
        values = [".mp4", ".mkv", ".ts", ".srt", ".ass", ".ssa", ".sup"]
    result: list[str] = []
    for value in values:
        try:
            extension = _normalise_extension(value)
        except ValueError:
            continue
        if extension not in result:
            result.append(extension)
    return result


def public_configuration(configuration: PluginConfiguration, *, plugin_id: str | None = None) -> dict[str, object]:
    """生成不包含任何管理员 Token 的前端配置投影。"""

    instances: list[dict[str, object]] = []
    for instance in configuration.instances:
        mappings: list[dict[str, object]] = []
        for mapping in instance.mappings:
            effective = (
                host_default_extensions() if mapping.whitelist_mode == "follow_host" else list(mapping.extensions)
            )
            mappings.append(
                {
                    "source_dir": mapping.source_dir,
                    "output_dir": mapping.output_dir,
                    "whitelist_mode": mapping.whitelist_mode,
                    "extensions": list(mapping.extensions),
                    "effective_extensions": effective,
                    "exclude_keywords": list(mapping.exclude_keywords),
                    "output_directory_warning": OUTPUT_DIRECTORY_WARNING,
                }
            )
        instances.append(
            {
                "id": instance.id,
                "name": instance.name,
                "scan_url": instance.scan_url,
                "playback_url": instance.playback_url or "",
                "api_token_configured": bool(instance.api_token),
                "mappings": mappings,
            }
        )
    result: dict[str, object] = {
        "instances": instances,
        "schedule_enabled": configuration.schedule_enabled,
        "schedule_cron": configuration.schedule_cron,
        "history_retention_days": configuration.history_retention_days,
        "notices": {
            "admin_token": ADMIN_TOKEN_WARNING,
            "root": ROOT_WARNING,
            "sign": SIGN_WARNING,
            "output_directory": OUTPUT_DIRECTORY_WARNING,
        },
    }
    if plugin_id:
        result["plugin_id"] = plugin_id
    return result


__all__ = [
    "ADMIN_TOKEN_WARNING",
    "DEFAULT_HISTORY_RETENTION_DAYS",
    "DEFAULT_SCHEDULE_CRON",
    "OUTPUT_DIRECTORY_WARNING",
    "ROOT_WARNING",
    "SIGN_WARNING",
    "ConfigurationValidationError",
    "DirectoryMapping",
    "OpenListInstance",
    "PluginConfiguration",
    "build_configuration",
    "find_output_conflicts",
    "host_default_extensions",
    "public_configuration",
    "secret_tokens",
]
