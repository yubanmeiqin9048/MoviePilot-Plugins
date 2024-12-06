from asyncio import Semaphore, run, to_thread
from typing import Any, Dict, List, Optional, Tuple, Union

from app import schemas
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class FileDistribution(_PluginBase):
    # 插件名称
    plugin_name = "文件分发"
    # 插件描述
    plugin_desc = "根据文件命分发文件到指定目录"
    # 插件图标
    plugin_icon = "sync_file.png"
    # 插件版本
    plugin_version = "1.2"
    # 插件作者
    plugin_author = "yubanmeiqin9048"
    # 作者主页
    author_url = "https://github.com/yubanmeiqin9048"
    # 插件配置项ID前缀
    plugin_config_prefix = "filedistribution_"
    # 加载顺序
    plugin_order = 68
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 下载器
    _downloader = None
    _save_path = None

    def init_plugin(self, config: dict = None):
        if not config:
            return
        self._enabled = config.get("enabled")
        self._save_path = config.get("save_path", None)
        self._downloader = config.get("downloader", None)
        if not self.downloader:
            self._enabled = False
            self.__update_config()
            return

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_page(self) -> List[dict]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        downloader_options = [
            {"title": config.name, "value": config.name}
            for config in self.downloader_helper.get_configs().values()
            if config.type == "qbittorrent"
        ]
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "savepath",
                                            "label": "保存路径",
                                            "hint": "输入可访问路径",
                                            "persistent-hint": True,
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
                                            "model": "downloader",
                                            "label": "下载器",
                                            "items": downloader_options,
                                            "hint": "选择下载器",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            },
        ], {"enable": False, "savepath": ""}

    def stop_service(self):
        """
        退出插件
        """
        pass
