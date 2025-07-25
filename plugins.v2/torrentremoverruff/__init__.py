import re
import shutil
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Literal, Optional, cast

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel
from qbittorrentapi import TorrentDictionary
from transmission_rpc import Torrent

from app.core.config import settings
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas import NotificationType, ServiceInfo
from app.utils.string import StringUtils

lock = threading.Lock()


class TorrentInfo(BaseModel):
    id: str
    date_done: datetime
    torrent_seeding_time: int
    uploaded: float
    size: int
    ratio: float
    upspeed: float
    need_delete: bool
    path: str
    trackers: list[str]
    state: Optional[str]
    category: Optional[str]
    site: str
    name: str
    error_string: Optional[str]

    def __hash__(self):
        return hash((self.id, self.name, self.site, self.size))

    def __eq__(self, other):
        if not isinstance(other, TorrentInfo):
            return False
        return self.id == other.id and self.name == other.name and self.site == other.site and self.size == other.size


class TorrentRemoverRuff(_PluginBase):
    # 插件名称
    plugin_name = "自动删种(ruff版)"
    # 插件描述
    plugin_desc = "自动删除下载器中的下载任务，基于官方插件二次开发。"
    # 插件图标
    plugin_icon = "delete.jpg"
    # 插件版本
    plugin_version = "2.4"
    # 插件作者
    plugin_author = "jxxghp,yubanmeiqin9048"
    # 作者主页
    author_url = "https://github.com/yubanmeiqin9048"
    # 插件配置项ID前缀
    plugin_config_prefix = "torrentremoverruff_"
    # 加载顺序
    plugin_order = 8
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _event = threading.Event()

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._downloaders = []
        self._scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: Optional[dict] = None):
        if config:
            self._enabled: bool = config.get("enabled", False)
            self._onlyonce: bool = config.get("onlyonce", False)
            self._notify: bool = config.get("notify", False)
            self._downloaders: list[str] = config.get("downloaders", [])
            self._action: Literal["pause", "delete", "deletefile"] = config.get("action", "pause")
            self._cron: str = config.get("cron", "")
            self._samedata: bool = config.get("samedata", False)
            self._mponly: bool = config.get("mponly", False)
            self._size: str = config.get("size", "")
            self._ratio: str = config.get("ratio", "")
            self._time: str = config.get("time", "")
            self._upspeed: str = config.get("upspeed", "")
            self._labels: str = config.get("labels", "")
            self._pathkeywords = config.get("pathkeywords", "")
            self._trackerkeywords: str = config.get("trackerkeywords", "")
            self._errorkeywords: str = config.get("errorkeywords", "")
            self._torrentstates: str = config.get("torrentstates", "")
            self._torrentcategorys: str = config.get("torrentcategorys", "")
            self._freespace_detect_path: str = config.get("freespace_detect_path", "")
            self._connection: Literal["and", "or"] = config.get("connection", "and")
            self._remove_mode: Literal["strategy", "condition"] = config.get("remove_mode", "condition")
            self._strategy: Literal["freespace", "maximum_count_seeds", "maximum_size_seeds"] = config.get(
                "strategy", "freespace"
            )
            self._strategy_value = float(config.get("strategy_value", 0))
            self._strategy_action: Literal["old_seeds", "small_seeds", "inactive_seeds"] = config.get(
                "strategy_action", "old_seeds"
            )
            self._complateonly: bool = config.get("complateonly", False)
        self.stop_service()

        if self.get_state() or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("自动删种服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self.delete_torrents,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                )
                # 关闭一次性开关
                self._onlyonce = False
                # 保存设置
                self.update_config(
                    {
                        "enabled": self._enabled,
                        "notify": self._notify,
                        "onlyonce": self._onlyonce,
                        "action": self._action,
                        "cron": self._cron,
                        "downloaders": self._downloaders,
                        "samedata": self._samedata,
                        "mponly": self._mponly,
                        "size": self._size,
                        "ratio": self._ratio,
                        "time": self._time,
                        "upspeed": self._upspeed,
                        "labels": self._labels,
                        "pathkeywords": self._pathkeywords,
                        "trackerkeywords": self._trackerkeywords,
                        "errorkeywords": self._errorkeywords,
                        "torrentstates": self._torrentstates,
                        "torrentcategorys": self._torrentcategorys,
                        "freespace_detect_path": self._freespace_detect_path,
                        "connection": self._connection,
                        "strategy": self._strategy,
                        "strategy_value": self._strategy_value,
                        "strategy_action": self._strategy_action,
                        "remove_mode": self._remove_mode,
                        "complateonly": self._complateonly,
                    }
                )
                if self._scheduler and self._scheduler.get_jobs():
                    # 启动服务
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return True if self._enabled and self._cron and self._downloaders else False

    @staticmethod
    def get_command() -> list[dict[str, Any]]:  # type: ignore
        pass

    def get_api(self) -> list[dict[str, Any]]:  # type: ignore
        pass

    def get_service(self) -> list[dict[str, Any]]:  # type: ignore
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self.get_state():
            return [
                {
                    "id": "TorrentRemover",
                    "name": "自动删种服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.delete_torrents,
                    "kwargs": {},
                }
            ]
        return []

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "samedata",
                                            "label": "处理辅种",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "mponly",
                                            "label": "仅MoviePilot任务",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "complateonly",
                                            "label": "仅已完成的种子",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {"model": "cron", "label": "执行周期", "placeholder": "0 */12 * * *"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "freespace_detect_path",
                                            "label": "硬盘容量检测路径",
                                        },
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "connection",
                                            "label": "条件连接器",
                                            "items": [{"title": "OR", "value": "or"}, {"title": "AND", "value": "and"}],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "downloaders",
                                            "label": "下载器",
                                            "items": [
                                                {"title": config.name, "value": config.name}
                                                for config in DownloaderHelper().get_configs().values()
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "remove_mode",
                                            "label": "删种模式",
                                            "items": [
                                                {"title": "条件模式", "value": "condition"},
                                                {"title": "策略模式", "value": "strategy"},
                                            ],
                                        },
                                    },
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "action",
                                            "label": "动作",
                                            "items": [
                                                {"title": "暂停", "value": "pause"},
                                                {"title": "删除种子", "value": "delete"},
                                                {"title": "删除种子和文件", "value": "deletefile"},
                                            ],
                                        },
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VTabs",
                        "props": {
                            "model": "_tabs",
                            "style": {"margin-top": "8px", "margin-bottom": "16px"},
                            "stacked": True,
                            "fixed-tabs": True,
                        },
                        "content": [
                            {"component": "VTab", "props": {"value": "strategy_tab"}, "text": "策略模式"},
                            {"component": "VTab", "props": {"value": "condition_tab"}, "text": "条件模式"},
                        ],
                    },
                    {
                        "component": "VWindow",
                        "props": {"model": "_tabs"},
                        "content": [
                            {
                                "component": "VWindowItem",
                                "props": {"value": "strategy_tab"},
                                "content": [
                                    {
                                        "component": "VRow",
                                        "props": {"style": {"margin-top": "0px"}},
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [
                                                    {
                                                        "component": "VSelect",
                                                        "props": {
                                                            "model": "strategy",
                                                            "label": "策略",
                                                            "hint": "触发器",
                                                            "persistent-hint": True,
                                                            "items": [
                                                                {"title": "最小剩余空间", "value": "freespace"},
                                                                {
                                                                    "title": "最大做种体积",
                                                                    "value": "maximum_size_seeds",
                                                                },
                                                                {
                                                                    "title": "最大做种数量",
                                                                    "value": "maximum_count_seeds",
                                                                },
                                                            ],
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [
                                                    {
                                                        "component": "VSelect",
                                                        "props": {
                                                            "model": "strategy_action",
                                                            "label": "策略动作",
                                                            "hint": "删种优先级",
                                                            "persistent-hint": True,
                                                            "items": [
                                                                {"title": "活动时间长的种子", "value": "old_seeds"},
                                                                {
                                                                    "title": "体积相较小的种子",
                                                                    "value": "small_seeds",
                                                                },
                                                                {
                                                                    "title": "较为不活跃的种子",
                                                                    "value": "inactive_seeds",
                                                                },
                                                            ],
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "strategy_value",
                                                            "label": "阈值",
                                                            "hint": "低于或高于此值则触发，空间单位为GB",
                                                            "persistent-hint": True,
                                                            "type": "number",
                                                            "min": "0",
                                                        },
                                                    }
                                                ],
                                            },
                                        ],
                                    }
                                ],
                            },
                            {
                                "component": "VWindowItem",
                                "props": {"value": "condition_tab"},
                                "content": [
                                    {
                                        "component": "VRow",
                                        "props": {"style": {"margin-top": "0px"}},
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 6},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "size",
                                                            "label": "种子大小（GB）",
                                                            "placeholder": "例如1-10",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 6},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "ratio",
                                                            "label": "分享率",
                                                            "placeholder": "",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 6},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "time",
                                                            "label": "做种时间（小时）",
                                                            "placeholder": "",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 6},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "upspeed",
                                                            "label": "平均上传速度",
                                                            "placeholder": "",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 6},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "labels",
                                                            "label": "标签",
                                                            "placeholder": "用,分隔多个标签",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 6},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "pathkeywords",
                                                            "label": "保存路径关键词",
                                                            "placeholder": "支持正式表达式",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 6},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "trackerkeywords",
                                                            "label": "Tracker关键词",
                                                            "placeholder": "支持正式表达式",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 6},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "errorkeywords",
                                                            "label": "错误信息关键词（TR）",
                                                            "placeholder": "支持正式表达式，仅适用于TR",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 6},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "torrentstates",
                                                            "label": "任务状态（QB）",
                                                            "placeholder": "用,分隔多个状态，仅适用于QB",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 6},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "torrentcategorys",
                                                            "label": "任务分类",
                                                            "placeholder": "用,分隔多个分类",
                                                        },
                                                    }
                                                ],
                                            },
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "style": {"margin-top": "16px"},
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "自动删种存在风险，如设置不当可能导致数据丢失！建议动作先选择暂停，确定条件正确后再改成删除。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "任务状态（QB）字典："
                                            "downloading：正在下载-传输数据，"
                                            "stalledDL：正在下载_未建立连接，"
                                            "uploading：正在上传-传输数据，"
                                            "stalledUP：正在上传-未建立连接，"
                                            "error：暂停-发生错误，"
                                            "pausedDL：暂停-下载未完成，"
                                            "pausedUP：暂停-下载完成，"
                                            "missingFiles：暂停-文件丢失，"
                                            "checkingDL：检查中-下载未完成，"
                                            "checkingUP：检查中-下载完成，"
                                            "checkingResumeData：检查中-启动时恢复数据，"
                                            "forcedDL：强制下载-忽略队列，"
                                            "queuedDL：等待下载-排队，"
                                            "forcedUP：强制上传-忽略队列，"
                                            "queuedUP：等待上传-排队，"
                                            "allocating：分配磁盘空间，"
                                            "metaDL：获取元数据，"
                                            "moving：移动文件，"
                                            "unknown：未知状态",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "action": "pause",
            "downloaders": [],
            "cron": "0 */12 * * *",
            "samedata": False,
            "mponly": False,
            "size": "",
            "ratio": "",
            "time": "",
            "upspeed": "",
            "labels": "",
            "pathkeywords": "",
            "trackerkeywords": "",
            "errorkeywords": "",
            "torrentstates": "",
            "torrentcategorys": "",
            "connection": "and",
            "strategy": "freespace",
            "strategy_action": "old_seeds",
            "strategy_value": 0.0,
            "remove_mode": "condition",
            "freespace_detect_path": "",
            "complateonly": False,
        }

    def get_page(self) -> list[dict]:  # type: ignore
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))

    @property
    def service_infos(self) -> Optional[dict[str, ServiceInfo]]:
        """
        服务信息
        """
        if not self._downloaders:
            logger.warning("尚未配置下载器，请检查配置")
            return None

        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        if not services:
            logger.warning("获取下载器实例失败，请检查配置")
            return None

        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance and not service_info.instance.is_inactive():
                active_services[service_name] = service_info
            else:
                logger.warning(f"下载器 {service_name} 未连接，请检查配置")

        if not active_services:
            logger.warning("没有已连接的下载器，请检查配置")
            return None

        return active_services

    def __get_downloader(self, name: str) -> Qbittorrent | Transmission:
        """
        根据类型返回下载器实例
        """
        if not self.service_infos:
            raise NotImplementedError("未初始化下载器")
        try:
            if downloader := self.service_infos[name].instance:
                return downloader
            raise NotImplementedError(f"未找到下载器：{name}")
        except KeyError as e:
            logger.error(f"未找到下载器：{name}")
            raise e

    def delete_torrents(self):
        """
        定时删除下载器中的下载任务
        """
        for downloader in self._downloaders:
            try:
                with lock:
                    # 获取需删除种子列表
                    torrents = self.get_remove_torrents(downloader)
                    logger.info(f"自动删种任务 获取符合处理条件种子数 {len(torrents)}")
                    # 下载器
                    downlader_obj = self.__get_downloader(downloader)
                    message_text = self._handle_action(downloader, downlader_obj, torrents)
                    if torrents and message_text and self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage, title="【自动删种任务完成】", text=message_text
                        )
            except Exception as e:
                logger.error(f"自动删种任务异常：{str(e)}")

    def _handle_action(self, downloader: str, downlader_obj: Qbittorrent | Transmission, torrents: set[TorrentInfo]):
        if self._action == "pause":
            message_text = f"{downloader.title()} 共暂停 {len(torrents)} 个种子"
            for torrent in torrents:
                if self._event.is_set():
                    logger.info("自动删种服务停止")
                    return message_text
                text_item = f"{torrent.name} 来自站点：{torrent.site} 大小：{StringUtils.str_filesize(torrent.size)}"
                downlader_obj.stop_torrents(ids=[torrent.id])
                logger.info(f"自动删种任务 暂停种子：{text_item}")
                message_text = f"{message_text}\n{text_item}"
            return message_text
        elif self._action == "delete":
            message_text = f"{downloader.title()} 共删除 {len(torrents)} 个种子"
            for torrent in torrents:
                if self._event.is_set():
                    logger.info("自动删种服务停止")
                    return message_text
                text_item = f"{torrent.name} 来自站点：{torrent.site} 大小：{StringUtils.str_filesize(torrent.size)}"
                downlader_obj.delete_torrents(delete_file=False, ids=[torrent.id])
                logger.info(f"自动删种任务 删除种子：{text_item}")
                message_text = f"{message_text}\n{text_item}"
            return message_text
        elif self._action == "deletefile":
            message_text = f"{downloader.title()} 共删除 {len(torrents)} 个种子及文件"
            for torrent in torrents:
                if self._event.is_set():
                    logger.info("自动删种服务停止")
                    return message_text
                text_item = f"{torrent.name} 来自站点：{torrent.site} 大小：{StringUtils.str_filesize(torrent.size)}"
                downlader_obj.delete_torrents(delete_file=True, ids=[torrent.id])
                logger.info(f"自动删种任务 删除种子及文件：{text_item}")
                message_text = f"{message_text}\n{text_item}"
            return message_text
        else:
            raise ValueError("未知操作")

    def old_seeds(self, torrents: list[Torrent] | list[TorrentDictionary]) -> list[Torrent | TorrentDictionary]:
        """
        旧的种子：按做种时间从大到小排序
        """

        def get_seeding_time(torrent: Torrent | TorrentDictionary) -> int:
            if isinstance(torrent, TorrentDictionary):
                date_done = torrent.completion_on if torrent.completion_on > 0 else torrent.added_on
                date_now = int(time.mktime(datetime.now().timetuple()))
                return date_now - date_done if date_done else 0
            else:
                date_done = torrent.date_done or torrent.date_added
                date_now = int(time.mktime(datetime.now().timetuple()))
                return date_now - int(time.mktime(date_done.timetuple())) if date_done else 0

        sorted_torrents = sorted(torrents, key=get_seeding_time, reverse=True)
        return sorted_torrents

    def small_seeds(self, torrents: list[Torrent] | list[TorrentDictionary]) -> list[Torrent | TorrentDictionary]:
        """
        体积小的种子：按体积从小到大排序
        """

        def get_size(torrent: Torrent | TorrentDictionary) -> int:
            if isinstance(torrent, TorrentDictionary):
                return torrent.size
            else:
                return torrent.total_size

        sorted_torrents = sorted(torrents, key=get_size)
        return sorted_torrents

    def inactive_seeds(self, torrents: list[Torrent] | list[TorrentDictionary]) -> list[Torrent | TorrentDictionary]:
        """
        不活跃的种子：按平均上传速度从小到大排序
        """

        def get_upspeed(torrent: Torrent | TorrentDictionary):
            if isinstance(torrent, TorrentDictionary):
                date_done = torrent.completion_on if torrent.completion_on > 0 else torrent.added_on
                date_now = int(time.mktime(datetime.now().timetuple()))
                seeding_time = date_now - date_done if date_done else 0
                uploaded = torrent.uploaded
                return uploaded / seeding_time if seeding_time else 0
            else:
                date_done = torrent.date_done or torrent.date_added
                date_now = int(time.mktime(datetime.now().timetuple()))
                seeding_time = date_now - int(time.mktime(date_done.timetuple())) if date_done else 0
                size = torrent.total_size
                ratio = torrent.ratio
                uploaded = ratio * size
                return uploaded / seeding_time if seeding_time else 0

        sorted_torrents = sorted(torrents, key=get_upspeed)
        return sorted_torrents

    def __need_delete(self, torrent: TorrentInfo, is_qb: bool) -> bool:  # noqa: C901
        if self._remove_mode == "strategy":
            return False
        connect_type = any if self._connection == "or" else all
        sizes = self._size.split("-") if self._size else []
        minsize = float(sizes[0]) * 1024**3 if sizes else 0
        maxsize = float(sizes[-1]) * 1024**3 if sizes else 0
        conditions: list[bool] = []
        if self._ratio:  # 分享率条件
            conditions.append(torrent.ratio >= float(self._ratio))
        if self._time:  # 做种时间条件
            conditions.append(torrent.torrent_seeding_time > float(self._time) * 3600)
        if self._size:  # 文件大小条件
            if not maxsize == minsize:
                conditions.append(int(minsize) <= torrent.size <= int(maxsize))
            else:
                conditions.append(torrent.size >= int(minsize))
        if self._upspeed:  # 上传速度条件
            conditions.append(torrent.upspeed >= float(self._upspeed) * 1024)
        if self._pathkeywords:  # 路径匹配条件
            conditions.append(len(re.findall(self._pathkeywords, torrent.path, re.I)) > 0)
        if self._trackerkeywords:  # Tracker匹配条件
            conditions.append(any(len(re.findall(self._trackerkeywords, str(t), re.I)) > 0 for t in torrent.trackers))
        if self._torrentstates and torrent.state and is_qb:  # 状态条件
            conditions.append(torrent.state in self._torrentstates.split(","))
        if self._torrentcategorys and torrent.category and is_qb:  # 分类条件
            conditions.append(torrent.category in self._torrentcategorys.split(","))
        if self._errorkeywords and torrent.error_string and not is_qb:  # 错误条件
            conditions.append(len(re.findall(self._errorkeywords, torrent.error_string, re.I)) > 0)
        return connect_type(conditions)

    def __fromat_torrent_info(self, torrent: Torrent | TorrentDictionary) -> TorrentInfo:
        """
        检查下载任务是否符合条件
        """
        is_qb = False
        date_now = int(time.mktime(datetime.now().timetuple()))

        if isinstance(torrent, TorrentDictionary):
            # QB字段
            is_qb = True
            date_done = torrent.completion_on if torrent.completion_on > 0 else torrent.added_on
            torrent_seeding_time = date_now - date_done if date_done else 0
            uploaded = torrent.uploaded
            size = torrent.size
            ratio = torrent.ratio
            upspeed = uploaded / torrent_seeding_time if torrent_seeding_time else 0
            path = torrent.save_path
            trackers = [
                str(t["url"]) for t in torrent.trackers if t["url"] not in ["** [LSD] **", "** [PeX] **", "** [DHT] **"]
            ]
            state = torrent.state
            category = torrent.category
            site = StringUtils.get_url_sld(str(trackers[0]))
            hash_id = torrent.hash
            name = torrent.name
            error_string = None
        else:
            # TR字段
            date_done = torrent.date_done or torrent.date_added
            torrent_seeding_time = date_now - int(time.mktime(date_done.timetuple())) if date_done else 0
            size = torrent.total_size
            ratio = torrent.ratio
            uploaded = ratio * size
            upspeed = uploaded / torrent_seeding_time if torrent_seeding_time else 0
            path = cast(str, torrent.download_dir)
            trackers = [t.announce for t in torrent.trackers]
            site = torrent.trackers[0].get("sitename") if trackers else ""
            error_string = torrent.error_string
            hash_id = torrent.hashString
            name = torrent.name
            state = None
            category = None

        torrent_info = TorrentInfo(
            date_done=date_done,
            torrent_seeding_time=torrent_seeding_time,
            size=size,
            ratio=ratio,
            uploaded=uploaded,
            upspeed=upspeed,
            path=path,
            trackers=trackers,
            site=site,
            error_string=error_string,
            id=hash_id,
            name=name,
            state=state,
            category=category,
            need_delete=False,
        )
        torrent_info.need_delete = self.__need_delete(torrent_info, is_qb=is_qb)
        return torrent_info

    def get_remove_torrents(self, downloader: str) -> set[TorrentInfo]:
        """
        获取自动删种任务种子
        """
        # 下载器对象
        downloader_obj = self.__get_downloader(downloader)
        tags = self._labels.split(",") if self._labels else []
        if self._mponly:
            tags.append(settings.TORRENT_TAG)
        # 查询种子
        if self._complateonly:
            torrents = downloader_obj.get_completed_torrents(tags=tags or None)
            error_flag = torrents is not None
        else:
            torrents, error_flag = downloader_obj.get_torrents(tags=tags or None)
        if error_flag:
            return set()
        if self._remove_mode == "condition":
            return self._get_remove_torrents_by_condition(cast(list, torrents))
        elif self._remove_mode == "strategy":
            return self._get_remove_torrents_by_strategy(cast(list, torrents))
        else:
            raise ValueError(f"未知删种模式 {self._remove_mode}")

    def _get_remove_torrents_by_condition(self, torrents: list[Torrent] | list[TorrentDictionary]) -> set[TorrentInfo]:
        group_map: defaultdict[tuple[str, int], set[TorrentInfo]] = defaultdict(set)
        remove_torrents: set[TorrentInfo] = set()
        remove_keys: set[tuple[str, int]] = set()
        for torrent in torrents:
            item = self.__fromat_torrent_info(torrent)
            if self._samedata:
                key = (item.name, item.size)
                group_map[key].add(item)
            if item.need_delete:
                remove_torrents.add(item)
                if self._samedata:
                    remove_keys.add(key)
        if self._samedata:
            for key in remove_keys:
                remove_torrents.update(group_map[key])
        return remove_torrents

    def _get_sorted_torrents(
        self, torrents: list[Torrent] | list[TorrentDictionary]
    ) -> list[Torrent | TorrentDictionary]:
        """获取排序后的种子列表"""
        if self._strategy_action == "inactive_seeds":
            return self.inactive_seeds(torrents)
        elif self._strategy_action == "old_seeds":
            return self.old_seeds(torrents)
        elif self._strategy_action == "small_seeds":
            return self.small_seeds(torrents)
        else:
            raise ValueError(f"未知策略动作 {self._strategy_action}")

    def _get_remove_torrents_by_strategy(self, torrents: list[Torrent] | list[TorrentDictionary]) -> set[TorrentInfo]:
        sorted_torrents = self._get_sorted_torrents(torrents)
        if self._strategy == "freespace":
            return self._remove_by_freespace(sorted_torrents)
        elif self._strategy == "maximum_count_seeds":
            return self._remove_by_maximum_count(sorted_torrents)
        elif self._strategy == "maximum_size_seeds":
            return self._remove_by_maximum_size(sorted_torrents)
        else:
            raise ValueError(f"未知策略{self._strategy}")

    def _remove_by_freespace(self, sorted_torrents: list[Torrent | TorrentDictionary]) -> set[TorrentInfo]:
        """限制最小磁盘容量策略"""
        group_map: defaultdict[tuple[str, int], set[TorrentInfo]] = defaultdict(set)
        remove_torrents: set[TorrentInfo] = set()
        remove_keys: set[tuple[str, int]] = set()
        free = shutil.disk_usage(self._freespace_detect_path).free / (1024**3)  # 单位GB
        if free > self._strategy_value:
            return set()
        # 计算需要释放的磁盘空间
        need_space = self._strategy_value - free
        for torrent in sorted_torrents:
            item = self.__fromat_torrent_info(torrent)
            item.need_delete = need_space > 0
            need_space -= item.size / (1024**3)
            if self._samedata:
                key = (item.name, item.size)
                group_map[key].add(item)
            if item.need_delete:
                remove_torrents.add(item)
                if self._samedata:
                    remove_keys.add(key)
            # 不处理辅种时提前返回
            if need_space <= 0 and not self._samedata:
                break
        if self._samedata:
            for key in remove_keys:
                remove_torrents.update(group_map[key])
        return remove_torrents

    def _remove_by_maximum_count(self, sorted_torrents: list[Torrent | TorrentDictionary]) -> set[TorrentInfo]:
        """限制最大种子数量策略"""
        group_map: defaultdict[tuple[str, int], set[TorrentInfo]] = defaultdict(set)
        remove_torrents: set[TorrentInfo] = set()
        remove_keys: set[tuple[str, int]] = set()
        current_count = len(sorted_torrents)
        if current_count <= int(self._strategy_value):
            return set()
        remove_count = current_count - int(self._strategy_value)
        for i, torrent in enumerate(sorted_torrents):
            item = self.__fromat_torrent_info(torrent)
            item.need_delete = not (i >= remove_count)
            if self._samedata:
                key = (item.name, item.size)
                group_map[key].add(item)
            if item.need_delete:
                remove_torrents.add(item)
                if self._samedata:
                    remove_keys.add(key)
            # 不处理辅种时提前返回
            if i >= remove_count and not self._samedata:
                break
        if self._samedata:
            for key in remove_keys:
                remove_torrents.update(group_map[key])
        return remove_torrents

    def _remove_by_maximum_size(self, sorted_torrents: list[Torrent | TorrentDictionary]) -> set[TorrentInfo]:
        """限制最大种子总大小策略"""
        group_map: defaultdict[tuple[str, int], set[TorrentInfo]] = defaultdict(set)
        remove_torrents: set[TorrentInfo] = set()
        remove_keys: set[tuple[str, int]] = set()
        total_size = sum(
            torrent.size if isinstance(torrent, TorrentDictionary) else torrent.total_size
            for torrent in sorted_torrents
        ) / (1024**3)  # 转换为GB
        if total_size <= self._strategy_value:
            return set()
        need_remove_size = total_size - self._strategy_value
        for torrent in sorted_torrents:
            item = self.__fromat_torrent_info(torrent)
            item.need_delete = need_remove_size >= item.size / (1024**3)
            need_remove_size -= item.size / (1024**3)
            if self._samedata:
                key = (item.name, item.size)
                group_map[key].add(item)
            if item.need_delete:
                remove_torrents.add(item)
                if self._samedata:
                    remove_keys.add(key)
            # 不处理辅种时提前返回
            if need_remove_size <= 0 and not self._samedata:
                break
        if self._samedata:
            for key in remove_keys:
                remove_torrents.update(group_map[key])
        return remove_torrents
