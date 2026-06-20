from asyncio import to_thread
from pathlib import Path
from typing import Any

from app import schemas
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import AsyncRequestUtils
from torrentool.api import Torrent


class DownloaderApi(_PluginBase):
    # 插件名称
    plugin_name = "下载器API"
    # 插件描述
    plugin_desc = "外部调用API直接下载，不识别。"
    # 插件图标
    plugin_icon = "sync_file.png"
    # 插件版本
    plugin_version = "1.4.0"
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

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._save_path = ""
        self._downloader = ""

    def init_plugin(self, config: dict | None = None):
        if not config:
            return
        self._enabled = config.get("enabled", False)
        self._save_path = config.get("save_path", "") or ""
        self._downloader = config.get("downloader", "") or ""
        if not self._downloader:
            self._enabled = False
            self.__update_config()
            return

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        return []

    def get_api(self) -> list[dict[str, Any]]:
        return [
            {
                "path": "/download_torrent_notest",
                "endpoint": self.download_torrent,
                "methods": ["GET"],
                "summary": "下载种子",
                "description": "直接下载种子，不识别",
            }
        ]

    def get_page(self) -> list[dict]:  # pyright: ignore[reportReturnType]  # ty:ignore[empty-body]
        pass

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        downloader_options = [
            {"title": config.name, "value": config.name}
            for config in DownloaderHelper().get_configs().values()
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
                                            "model": "save_path",
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
        ], {"enable": False, "save_path": ""}

    def stop_service(self):
        """
        退出插件
        """
        pass

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "save_path": self._save_path,
                "downloader": self._downloader,
            }
        )

    async def download_torrent(self, torrent_url: str) -> schemas.Response:
        """
        API调用下载种子，使用标准下载链同步为与本体下载一致的逻辑
        """
        try:
            if not self._downloader:
                return schemas.Response(success=False, message="未配置下载器")

            # 下载种子文件（或识别磁力链接）
            torrent_content, error = await self.__fetch_torrent(torrent_url)
            if error:
                return schemas.Response(success=False, message=error)
            if not torrent_content:
                return schemas.Response(success=False, message="下载种子内容为空")

            # 使用标准下载链添加下载任务
            result = await to_thread(
                self.chain.download,
                content=torrent_content,
                download_dir=Path(self._save_path),
                cookie="",
                downloader=self._downloader,
            )

            if not result:
                return schemas.Response(
                    success=False,
                    message="添加下载失败，请检查下载器是否已启用且可连接",
                )

            _, torrent_hash, _, error_msg = result

            if not torrent_hash:
                return schemas.Response(
                    success=False,
                    message=error_msg or "添加下载失败",
                )

            # 发送下载添加事件
            self.eventmanager.send_event(
                EventType.PluginAction,
                {"action": "downloaderapi_add", "hash": torrent_hash},
            )

            return schemas.Response(
                success=True,
                message=f"添加下载成功: {torrent_hash}",
            )
        except Exception as e:
            logger.error(f"下载种子异常: {e}")
            return schemas.Response(success=False, message=f"调用失败，原因：{e}")

    async def __fetch_torrent(self, url: str) -> tuple[str | bytes | None, str | None]:
        """
        从URL下载种子文件内容
        :return: (torrent_content_or_magnet, error_msg)
        """
        # 磁力链接直接返回
        if url.startswith("magnet:"):
            return url, None

        try:
            req = await AsyncRequestUtils().get_res(url=url, allow_redirects=False)
            # 跟随重定向
            while req and req.status_code in [301, 302]:
                url = req.headers.get("Location", "")
                if url.startswith("magnet:"):
                    return url, None
                req = await AsyncRequestUtils().get_res(url=url, allow_redirects=False)

            if req is None:
                return None, "无法打开链接"
            if req.status_code != 200:
                return None, f"下载种子出错，状态码：{req.status_code}"
            if not req.content:
                return None, "未下载到种子数据"
            if req.content.startswith(b"magnet:"):
                return req.text, None

            # 验证是否为有效种子文件
            try:
                # torrentool 接受 bytes 和 str，类型桩可能只声明了 str
                Torrent.from_string(req.content)  # type: ignore[arg-type]  # ty:ignore[invalid-argument-type]
            except Exception:
                return None, "种子数据有误，请确认链接是否正确"

            return req.content, None
        except Exception as e:
            logger.error(f"下载种子文件异常: {e}")
            return None, f"下载种子文件失败: {e}"
