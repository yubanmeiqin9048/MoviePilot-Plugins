from pathlib import Path
from typing import Any, Dict, List, Tuple

from app import schemas
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.modules.qbittorrent.qbittorrent import Qbittorrent
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.string import StringUtils


class DownloaderApi(_PluginBase):
    # 插件名称
    plugin_name = "下载API"
    # 插件描述
    plugin_desc = "外部调用API直接下载，不识别。"
    # 插件图标
    plugin_icon = "Themeengine_A.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "yubanmeiqin9048"
    # 作者主页
    author_url = "https://github.com/yubanmeiqin9048"
    # 插件配置项ID前缀
    plugin_config_prefix = "downloaderapi_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False

    def init_plugin(self, config: dict = None):
        pass

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/download_torrent_notest",
                "endpoint": self.api_download_torrent,
                "methods": ["POST"],
                "summary": "下载种子",
                "description": "直接下载种子，不识别",
            }
        ]

    def get_page(self) -> List[dict]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
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
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "fontpath",
                                            "label": "字体库路径",
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ], {"enable": False, "fontpath": ""}

    def stop_service(self):
        """
        退出插件
        """
        pass

    def api_download_torrent(self, torrent_url: str) -> schemas.Response:
        """
        API调用下载种子
        """
        # 添加下载
        tag = StringUtils.generate_random_str(10)
        # 直接使用下载器默认目录
        torrent = self.qbittorrent.add_torrent(content=torrent_url, tag=tag)

        if torrent:
            torrent_hash = self.qbittorrent.get_torrent_id_by_tag(tags=tag)
            if torrent_hash:
                return schemas.Response(success=True, message="下载成功")
            else:
                return schemas.Response(
                    success=True, message="下载成功, 但获取种子hash失败"
                )
        return schemas.Response(success=False, message="种子添加下载失败")
