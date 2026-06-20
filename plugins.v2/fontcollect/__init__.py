import subprocess
import time
import traceback
from pathlib import Path
from typing import Any

from app.core.event import eventmanager
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.modules.qbittorrent.qbittorrent import Qbittorrent
from app.plugins import _PluginBase
from app.schemas import ServiceInfo
from app.schemas.types import EventType
from qbittorrentapi import TorrentFilesList


class FontCollect(_PluginBase):
    # 插件名称
    plugin_name = "字体收集"
    # 插件描述
    plugin_desc = "自动收集种子中存在的字体。"
    # 插件图标
    plugin_icon = "Themeengine_A.png"
    # 插件版本
    plugin_version = "1.8.3"
    # 插件作者
    plugin_author = "yubanmeiqin9048"
    # 作者主页
    author_url = "https://github.com/yubanmeiqin9048"
    # 插件配置项ID前缀
    plugin_config_prefix = "fontcollect_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 2

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._downloader = ""

    def init_plugin(self, config: dict | None = None):
        if config:
            self._downloader: str = config.get("downloader", "") or ""
            self._enabled = config.get("enabled", False)
            self._fontpath = config.get("fontpath", "") or ""
            if not Path(self._fontpath).exists() or not self.downloader:
                logger.error("未配置字体库路径或下载器，插件退出")
                self._enabled = False
                self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:  # type: ignore
        pass

    def get_api(self) -> list[dict[str, Any]]:  # type: ignore
        pass

    def get_page(self) -> list[dict]:  # type: ignore
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
                                            "model": "fontpath",
                                            "label": "字体库路径",
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
        ], {"enable": False, "fontpath": ""}

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "fontpath": self._fontpath,
            }
        )

    def stop_service(self):
        """
        退出插件
        """
        pass

    def __wait_for_files_completion(self, torrent_hash: str, file_ids: list[str]):
        """
        长轮询等待文件下载完成
        """
        logger.info(f"开始等待{torrent_hash}")
        while True:
            try:
                files = self.downloader.get_files(torrent_hash)
                if not files:  # 获取文件列表失败
                    raise RuntimeError(f"获取 {torrent_hash} 文件列表失败")
                all_completed = all(file["progress"] == 1 for file in files if file["id"] in file_ids)
                if all_completed:
                    logger.info(f"{torrent_hash} 字体包下载完成")
                    time.sleep(5)
                    break
                time.sleep(5)  # 每隔5秒检查一次
            except Exception as e:
                raise RuntimeError(f"等待 {torrent_hash} 下载失败: {e}") from e

    def unzip_font_files(
        self,
        torrent_files: TorrentFilesList,
        font_file_ids: list[str],
        save_path: str,
    ):
        """
        解压下载完成的 Font 文件，使用 unar 支持 7z/rar/zip 等格式
        """
        font_files: list[str] = [file.name for file in torrent_files if file.id in font_file_ids]
        extract_dir = Path(self._fontpath)
        extract_dir.mkdir(parents=True, exist_ok=True)
        for font_file in font_files:
            file_path = Path(save_path) / font_file
            try:
                subprocess.run(
                    [
                        "unar",
                        "-quiet",
                        "-force-overwrite",
                        "-output-directory",
                        extract_dir.as_posix(),
                        file_path.as_posix(),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                logger.info(f"解压 {file_path} 到 {self._fontpath} 成功")
            except subprocess.CalledProcessError as e:
                logger.error(f"解压 {file_path} 失败：{e.stderr.strip() if e.stderr else e}")
            except FileNotFoundError:
                logger.error("unar 命令不可用，请确认系统已安装 unar")

    def collect(self, torrent_hash: str):
        """
        等待字体下载完成并解压
        """
        try:
            # 获取根目录
            torrent_info, _ = self.downloader.get_torrents(ids=torrent_hash)
            save_path = torrent_info[0].save_path
            # 获取种子文件
            torrent_files = self.downloader.get_files(torrent_hash)
            if not torrent_files:
                logger.error("获取种子文件失败，下载任务可能在暂停状态")
                return
            # 筛选文件名包含"Font"的文件
            font_file_ids = [torrent_file.id for torrent_file in torrent_files if "Font" in torrent_file.name]
            if not font_file_ids:
                return
            # 设置"Font"文件的优先级为最高
            self.downloader.set_files(torrent_hash=torrent_hash, file_ids=font_file_ids, priority=7)
            self.__wait_for_files_completion(torrent_hash, font_file_ids)
            self.unzip_font_files(torrent_files=torrent_files, font_file_ids=font_file_ids, save_path=save_path)
        except Exception as e:
            logger.debug(f"处理 {torrent_hash} 失败：{e} - {traceback.format_exc()}")

    @eventmanager.register(EventType.DownloadAdded)
    def process_inner(self, event):
        torrent_hash: str | None = event.event_data.get("hash")
        if not torrent_hash:
            return
        self.collect(torrent_hash=torrent_hash)

    @eventmanager.register(EventType.PluginAction)
    def process_outter(self, event):
        if event.event_data.get("action") != "downloaderapi_add":
            return
        torrent_hash: str | None = event.event_data.get("hash")
        if not torrent_hash:
            return
        self.collect(torrent_hash=torrent_hash)

    @property
    def service_info(self) -> ServiceInfo | None:
        """
        服务信息
        """
        if not self._downloader:
            logger.warning("尚未配置下载器，请检查配置")
            return None

        service = DownloaderHelper().get_service(name=self._downloader, type_filter="qbittorrent")
        if not service:
            logger.warning("获取下载器实例失败，请检查配置")
            return None
        if not service.instance:
            logger.warning("下载器实例为空，请检查配置")
            return None
        if service.instance.is_inactive():
            logger.warning(f"下载器 {self._downloader} 未连接，请检查配置")
            return None

        return service

    @property
    def downloader(self) -> Qbittorrent:
        """
        下载器实例
        """
        if self.service_info and self.service_info.instance:
            return self.service_info.instance
        raise Exception("下载器实例为空")
