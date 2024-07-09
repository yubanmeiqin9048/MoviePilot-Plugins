import time
import py7zr
import zipfile
import threading
from pathlib import Path
from typing import Any, List, Dict, Tuple

from app import schemas
from app.core.event import eventmanager, Event
from app.core.config import settings
from app.schemas.types import EventType
from app.modules.qbittorrent.qbittorrent import Qbittorrent
from app.plugins import _PluginBase
from app.utils.string import StringUtils
from app.log import logger

class FontCollect(_PluginBase):
    # 插件名称
    plugin_name = "字体收集"
    # 插件描述
    plugin_desc = "自动收集种子中存在的字体。"
    # 插件图标
    plugin_icon = "Themeengine_A.png"
    # 插件版本
    plugin_version = "1.3"
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

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._fontpath = config.get("fontpath")
            self.qbittorrent = Qbittorrent()


    def get_state(self) -> bool:
        return True if self._enabled and self._fontpath else False
    
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/download_torrent_notest",
            "endpoint": self.api_download_torrent,
            "methods": ["GET"],
            "summary": "下载种子",
            "description": "直接下载种子，不识别",
        }]

    def get_page(self) -> List[dict]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    # 'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'fontpath',
                                            'label': '字体库路径',
                                        }
                                    }
                                ]
                            }                      
                        ]
                    }
                ]
            }
        ], {
            "enable": False,
            "fontpath": ''
        }

    def collect(self, torrent_hash: str = None):
        """
        等待字体下载完成并解压
        """
        def __unzip_font_files(torrent_files: List[Dict[str, Any]], font_file_ids: List[int]):
            """
            解压下载完成的Font文件
            """
            font_files = [file for file in torrent_files if file["id"] in font_file_ids]
            for font_file in font_files:
                file_path = Path(save_path) / font_file["name"]
                try:
                    output_dir = Path(self._fontpath)
                    output_dir.mkdir(parents=True, exist_ok=True)

                    if file_path.suffix == ".zip":
                        with zipfile.ZipFile(file_path, 'r') as zip_ref:
                            zip_ref.extractall(output_dir)
                    elif file_path.suffix == ".7z":
                        with py7zr.SevenZipFile(file_path, mode='r') as z_ref:
                            z_ref.extractall(path=output_dir)
                    logger.info(f"解压 {file_path} 到 {output_dir} 成功")
                except Exception as e:
                    logger.error(f"解压 {file_path}失败，原因: {e}")

        def __wait_for_files_completion(torrent_hash: str, file_ids: List[int]):
            """
            长轮询等待文件下载完成
            """
            while True:
                try:
                    files = self.qbittorrent.get_files(torrent_hash)
                    all_completed = all(file["priority"] == 7 and file["progress"] == 1 for file in files if file["id"] in file_ids)
                    if all_completed:
                        self.qbittorrent.remove_torrents_tag(ids=torrent_hash, tag='')
                        break
                    time.sleep(5)  # 每隔5秒检查一次
                except Exception as e:
                    logger.error(f"检测失败, 原因:{e}")
                    break

        def __set_torrent_foce_resume_status(torrent_hash: str):
            """
            根据需要强制继续
            """
            if settings.QB_FORCE_RESUME:
                # 强制继续
                self.qbittorrent.torrents_set_force_start(torrent_hash)
            else:
                self.qbittorrent.start_torrents(torrent_hash)

        if not torrent_hash:
            logger.error(f"种子hash获取失败")
            return
        
        # 获取根目录
        torrent_info, _ = self.qbittorrent.get_torrents(ids=torrent_hash)
        save_path = torrent_info[0].get("save_path")
        # 筛选文件名包含“Font”的文件
        font_file_ids = []
        other_file_ids = []

        # 获取种子文件
        torrent_files = self.qbittorrent.get_files(torrent_hash)
        if not torrent_files:
            logger.error(f"获取种子文件失败，下载任务可能在暂停状态")
            return
        
        # 获取优先级大于1的文件
        need_files = [f for f in torrent_files if f.get('priority') >= 1]
        if not need_files:
            logger.error(f"种子中没有优先级大于1的文件")
            return

        # 暂停任务
        self.qbittorrent.stop_torrents(torrent_hash)

        for torrent_file in need_files:
            file_id = torrent_file.get("id")
            file_name = torrent_file.get("name")
            if "Font" in file_name:
                font_file_ids.append(file_id)
            else:
                other_file_ids.append(file_id)

        # 设置“Font”文件的优先级为最高
        if font_file_ids:
            self.qbittorrent.set_files(torrent_hash=torrent_hash, file_ids=font_file_ids, priority=7)
            self.qbittorrent.set_files(torrent_hash=torrent_hash, file_ids=other_file_ids, priority=0)

            # 恢复任务，只下载“Font”文件
            __set_torrent_foce_resume_status(torrent_hash=torrent_hash)

            __wait_for_files_completion(torrent_hash=torrent_hash, file_ids=font_file_ids)

            __unzip_font_files(torrent_files=torrent_files, font_file_ids=font_file_ids)

            self.qbittorrent.set_files(torrent_hash=torrent_hash, file_ids=other_file_ids, priority=1)

        return __set_torrent_foce_resume_status(torrent_hash=torrent_hash)

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
                threading.Thread(target=self.collect, args=(torrent_hash,)).start()
                return schemas.Response(success=True, message="下载成功")
            else:
                return schemas.Response(success=True, message="下载成功, 但获取种子hash失败")
        return schemas.Response(success=False, message="种子添加下载失败")

    @eventmanager.register(EventType.DownloadAdded)
    def process(self, event: Event):
        torrent_hash = event.event_data.get("hash")
        self.collect(torrent_hash=torrent_hash)

    def stop_service(self):
        """
        退出插件
        """
        pass