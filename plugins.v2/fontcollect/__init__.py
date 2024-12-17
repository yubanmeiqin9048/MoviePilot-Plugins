import os
import traceback
import zipfile
from asyncio import gather, run, sleep, to_thread
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import py7zr
from app.core.event import Event, eventmanager
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.modules.qbittorrent.qbittorrent import Qbittorrent
from app.modules.transmission.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas import ServiceInfo
from app.schemas.types import EventType


class FontCollect(_PluginBase):
    # 插件名称
    plugin_name = "字体收集"
    # 插件描述
    plugin_desc = "自动收集种子中存在的字体。"
    # 插件图标
    plugin_icon = "Themeengine_A.png"
    # 插件版本
    plugin_version = "1.6.1"
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

    # 私有属性
    _enabled = False
    _fontpath = ""
    _downloader = None

    def init_plugin(self, config: dict = None):
        self.downloader_helper = DownloaderHelper()
        self._downloader = config.get("downloader", None)
        if config:
            self._enabled = config.get("enabled")
            self._fontpath = config.get("fontpath")

            if not Path(self._fontpath).exists() or not self.downloader:
                logger.error("未配置字体库路径或下载器，插件退出")
                self._enabled = False
                self.__update_config()

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

    async def __wait_for_files_completion(self, torrent_hash: str, file_ids: List[int]):
        """
        长轮询等待文件下载完成
        """
        logger.info(f"开始等待{torrent_hash}")
        while True:
            try:
                files = await to_thread(self.downloader.get_files, torrent_hash)
                all_completed = all(
                    file["priority"] == 1 and file["progress"] == 1
                    for file in files
                    if file["id"] in file_ids
                )
                if all_completed:
                    logger.info(f"{torrent_hash} 字体包下载完成")
                    break
                await sleep(5)  # 每隔5秒检查一次
            except Exception as e:
                raise RuntimeError(f"等待 {torrent_hash} 下载失败: {e}")

    def __extract_zip(self, file_path: Path, output_dir: Path):
        with zipfile.ZipFile(file_path, "r") as zip_ref:
            zip_ref.extractall(output_dir)

    def __extract_7z(self, file_path: Path, output_dir: Path):
        with py7zr.SevenZipFile(file_path, mode="r") as z_ref:
            z_ref.extractall(path=output_dir)

    async def __unzip_single_file(self, file_path: Path, output_dir: Path):
        try:
            if not output_dir.exists():
                await to_thread(output_dir.mkdir(parents=True, exist_ok=True))

            if file_path.suffix == ".zip":
                await to_thread(self.__extract_zip, file_path, output_dir)
            elif file_path.suffix == ".7z":
                await to_thread(self.__extract_7z, file_path, output_dir)

            logger.info(f"解压 {file_path} 到 {output_dir} 成功")
        except Exception as e:
            logger.error(f"解压 {file_path} 失败，原因: {e}")

    async def unzip_font_files(
        self,
        torrent_files: List[Dict[str, Any]],
        font_file_ids: List[int],
        save_path: str,
    ):
        """
        解压下载完成的 Font 文件
        """
        font_files = [file for file in torrent_files if file["id"] in font_file_ids]
        tasks = []
        for font_file in font_files:
            file_path = os.path.join(save_path, font_file["name"])
            tasks.append(
                self.__unzip_single_file(Path(file_path), Path(self._fontpath))
            )
        await gather(*tasks)

    async def collect(self, torrent_hash: str = None):
        """
        等待字体下载完成并解压
        """

        def __set_torrent_foce_resume_status(torrent_hash: str):
            """
            根据需要强制继续
            """
            # 强制继续
            if self.downloader.torrents_set_force_start(torrent_hash):
                pass
            else:
                self.downloader.start_torrents(torrent_hash)

        if not torrent_hash:
            logger.error("种子hash获取失败")
            return
        try:
            # 获取根目录
            torrent_info, _ = self.downloader.get_torrents(ids=torrent_hash)
            save_path = torrent_info[0].get("save_path")
            # 筛选文件名包含“Font”的文件
            font_file_ids = []
            other_file_ids = []

            # 获取种子文件
            torrent_files = self.downloader.get_files(torrent_hash)
            if not torrent_files:
                logger.error("获取种子文件失败，下载任务可能在暂停状态")
                return

            # 暂停任务
            self.downloader.stop_torrents(torrent_hash)

            # 获取优先级大于1的文件
            for torrent_file in torrent_files:
                file_id = torrent_file.get("id")
                file_name = torrent_file.get("name")
                priority = torrent_file.get("priority")

                # 检查优先级条件
                if priority >= 1 or "Font" in file_name:
                    # 分类文件
                    if "Font" in file_name:
                        font_file_ids.append(file_id)
                    else:
                        other_file_ids.append(file_id)

            if not other_file_ids:
                logger.warning("种子中没有优先级大于1的文件")
                return

            if not font_file_ids:
                __set_torrent_foce_resume_status(torrent_hash=torrent_hash)
                return

            # 设置“Font”文件的优先级为最高
            self.downloader.set_files(
                torrent_hash=torrent_hash, file_ids=other_file_ids, priority=0
            )

            # 恢复任务，只下载“Font”文件
            __set_torrent_foce_resume_status(torrent_hash=torrent_hash)

            await self.__wait_for_files_completion(torrent_hash, font_file_ids)
            await self.unzip_font_files(
                torrent_files=torrent_files,
                font_file_ids=font_file_ids,
                save_path=save_path,
            )

            self.downloader.set_files(
                torrent_hash=torrent_hash, file_ids=other_file_ids, priority=1
            )

        except Exception as e:
            logger.debug(
                f"处理 {torrent_hash} 失败：{str(e)} - {traceback.format_exc()}"
            )

    @eventmanager.register(EventType.DownloadAdded)
    def process_inner(self, event: Event):
        torrent_hash = event.event_data.get("hash")
        run(self.collect(torrent_hash=torrent_hash))

    @eventmanager.register(EventType.PluginAction)
    def process_outter(self, event: Event):
        if event.event_data.get("action") != "downloaderapi_add":
            return
        torrent_hash = event.event_data.get("hash")
        run(self.collect(torrent_hash=torrent_hash))

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
