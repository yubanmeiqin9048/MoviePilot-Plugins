from typing import Any, Dict, List, Optional, Tuple, Union

from app import schemas
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.modules.qbittorrent.qbittorrent import Qbittorrent
from app.modules.transmission.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas import ServiceInfo


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
    plugin_order = 68
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 下载器
    _downloader = None

    def init_plugin(self, config: dict = None):
        if config:
            self.downloader_helper = DownloaderHelper()
            self._enabled = config.get("enabled")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass
        # return [
        #     {
        #         "path": "/download_torrent_notest",
        #         "endpoint": self.api_download_torrent,
        #         "methods": ["POST"],
        #         "summary": "下载种子",
        #         "description": "直接下载种子，不识别",
        #     }
        # ]

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
                            }
                        ],
                    }
                ],
            }
        ], {"enable": False}

    def stop_service(self):
        """
        退出插件
        """
        pass

    def api_download_torrent(self, torrent_url: str) -> schemas.Response:
        """
        API调用下载种子
        """
        pass
        # # 添加下载
        # tag = StringUtils.generate_random_str(10)
        # # 直接使用下载器默认目录
        # torrent = self.qbittorrent.add_torrent(content=torrent_url, tag=tag)

        # if torrent:
        #     torrent_hash = self.qbittorrent.get_torrent_id_by_tag(tags=tag)
        #     if torrent_hash:
        #         return schemas.Response(success=True, message="下载成功")
        #     else:
        #         return schemas.Response(
        #             success=True, message="下载成功, 但获取种子hash失败"
        #         )
        # return schemas.Response(success=False, message="种子添加下载失败")

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

    def check_downloader_type(self) -> bool:
        """
        检查下载器类型是否为 qbittorrent 或 transmission
        """
        if self.downloaderhelper.is_downloader(
            service_type="qbittorrent", service=self.service_info
        ):
            # 处理 qbittorrent 类型
            return True
        elif self.downloaderhelper.is_downloader(
            service_type="transmission", service=self.service_info
        ):
            # 处理 transmission 类型
            return True
        return False
