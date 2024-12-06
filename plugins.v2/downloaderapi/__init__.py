from asyncio import run, to_thread
from typing import Any, Dict, List, Optional, Tuple, Union

from app import schemas
from app.helper.downloader import DownloaderHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.modules.qbittorrent.qbittorrent import Qbittorrent
from app.modules.transmission.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas import ServiceInfo
from app.schemas.types import EventType
from app.utils.string import StringUtils


class DownloaderApi(_PluginBase):
    # 插件名称
    plugin_name = "下载器API"
    # 插件描述
    plugin_desc = "外部调用API直接下载，不识别。"
    # 插件图标
    # 插件图标
    plugin_icon = "sync_file.png"
    # 插件版本
    plugin_version = "1.2"
    # 插件作者
    plugin_author = "yubanmeiqin9048"
    # 作者主页
    author_url = "https://github.com/yubanmeiqin9048"
    # 插件配置项ID前缀
    plugin_config_prefix = "downloaderapi_"
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
        self.downloader_helper = DownloaderHelper()
        self.torrent_helper = TorrentHelper()
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
        return [
            {
                "path": "/download_torrent_notest",
                "endpoint": self.process,
                "methods": ["GET"],
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

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "fontpath": self._save_path,
            }
        )

    def process(self, torrent_url: str):
        return run(self.download_torrent(torrent_url))

    async def download_torrent(self, torrent_url: str) -> schemas.Response:
        """
        API调用下载种子
        """
        try:
            downloader = self.downloader
            # 添加下载
            tag = StringUtils.generate_random_str(10)
            file_path, content, _, _, _ = await to_thread(
                self.torrent_helper.download_torrent, torrent_url
            )
            state = (
                downloader.add_torrent(
                    content=content, download_dir=self._save_path, tag=tag
                )
                if content and file_path
                else None
            )
            torrent_hash = downloader.get_torrent_id_by_tag(tag)

            if not state:
                return schemas.Response(success=False, message="种子添加下载失败")
            else:
                self.eventmanager.send_event(
                    EventType.PluginAction,
                    {"action": "downloaderapi_add", "hash": f"{torrent_hash}"},
                )
                return schemas.Response(success=True, message="下载成功")
        except Exception as e:
            return schemas.Response(success=False, message=f"调用失败，原因：{e}")

    @property
    def service_info(self) -> Optional[ServiceInfo]:
        """
        服务信息
        """
        if not self._downloader:
            logger.warning("尚未配置下载器，请检查配置")
            return None

        service = self.downloader_helper.get_service(
            name=self._downloader, type_filter="qbittorrent"
        )
        if not service:
            logger.warning("获取下载器实例失败，请检查配置")
            return None

        if service.instance.is_inactive():
            logger.warning(f"下载器 {self._downloader} 未连接，请检查配置")
            return None

        return service

    @property
    def downloader(self) -> Optional[Union[Qbittorrent, Transmission]]:
        """
        下载器实例
        """
        return self.service_info.instance if self.service_info else None
