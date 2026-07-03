import json
import os
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.event import eventmanager
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.modules.qbittorrent.qbittorrent import Qbittorrent
from app.plugins import _PluginBase
from app.schemas import ServiceInfo
from app.schemas.types import EventType
from apscheduler.triggers.cron import CronTrigger
from fontTools.ttLib import TTFont, TTLibError
from qbittorrentapi import TorrentFilesList


class FontCollect(_PluginBase):
    # 插件名称
    plugin_name = "字体收集"
    # 插件描述
    plugin_desc = "自动收集种子中存在的字体。"
    # 插件图标
    plugin_icon = "Themeengine_A.png"
    # 插件版本
    plugin_version = "1.9.0"
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

    # 字体文件扩展名
    FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2"}

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._downloader = ""
        self._fontpath = ""
        self._dedup_enabled = False
        self._dedup_cron = ""
        self._dedup_action = "delete"  # delete / move
        self._dedup_keep = "newest"  # newest / largest
        self._rebuild_index = False
        # 字体索引：{font_name: [file_path, ...]}
        self._font_index: dict[str, list[str]] = {}

    def init_plugin(self, config: dict | None = None):
        if config:
            self._downloader: str = config.get("downloader", "") or ""
            self._enabled = config.get("enabled", False)
            self._fontpath = config.get("fontpath", "") or ""
            self._dedup_enabled = config.get("dedup_enabled", False)
            self._dedup_cron = config.get("dedup_cron", "") or "0 2 * * *"
            self._dedup_action = config.get("dedup_action", "delete") or "delete"
            self._dedup_keep = config.get("dedup_keep", "newest") or "newest"
            self._rebuild_index = config.get("rebuild_index", False)
            if not Path(self._fontpath).exists() or not self.downloader:
                logger.error("未配置字体库路径或下载器，插件退出")
                self._enabled = False
                self.__update_config()
            # 加载已有字体索引，避免全量扫盘
            self._load_font_index()
            # 用户触发重建索引
            if self._rebuild_index:
                logger.info("用户触发重建字体索引，将清除已有索引并全量重建")
                self._font_index = {}
                if self._font_index_file.exists():
                    try:
                        os.remove(self._font_index_file)
                    except OSError:
                        pass
                self.build_font_index()
                self._rebuild_index = False
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "dedup_enabled",
                                            "label": "启用字体去重",
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
                                            "model": "rebuild_index",
                                            "label": "重建字体索引",
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
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VDivider",
                                        "props": {},
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
                                        "component": "VCronField",
                                        "props": {
                                            "model": "dedup_cron",
                                            "label": "去重执行周期",
                                            "placeholder": "0 2 * * *",
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
                                            "model": "dedup_action",
                                            "label": "去重动作",
                                            "items": [
                                                {"title": "删除", "value": "delete"},
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
                                            "model": "dedup_keep",
                                            "label": "保留策略",
                                            "hint": "同名字体保留哪一个",
                                            "persistent-hint": True,
                                            "items": [
                                                {"title": "保留最新文件", "value": "newest"},
                                                {"title": "保留最大文件", "value": "largest"},
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "字体去重基于 fonttools 提取的字体名称进行比对，"
                                                "同名字体将按保留策略仅保留一份。"
                                            ),
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            },
        ], {
            "enabled": False,
            "fontpath": "",
            "dedup_enabled": False,
            "dedup_cron": "0 2 * * *",
            "dedup_action": "delete",
            "dedup_keep": "newest",
            "rebuild_index": False,
        }

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "fontpath": self._fontpath,
                "dedup_enabled": self._dedup_enabled,
                "dedup_cron": self._dedup_cron,
                "dedup_action": self._dedup_action,
                "dedup_keep": self._dedup_keep,
                "rebuild_index": self._rebuild_index,
            }
        )

    def stop_service(self):
        """
        退出插件，保存字体索引并停止调度
        """
        self._save_font_index()

    def get_service(self) -> list[dict[str, Any]]:
        """
        注册插件公共服务 - 字体去重后台任务
        """
        if self.get_state() and self._dedup_enabled and self._dedup_cron:
            return [
                {
                    "id": "FontDeduplicate",
                    "name": "字体去重服务",
                    "trigger": CronTrigger.from_crontab(self._dedup_cron),
                    "func": self.deduplicate_fonts,
                    "kwargs": {},
                }
            ]
        return []

    # ------------------------------- 字体索引 ------------------------------- #

    @property
    def _font_index_file(self) -> Path:
        """字体索引文件路径，存放在字体库目录下"""
        return Path(self._fontpath) / ".font_index.json"

    def _load_font_index(self) -> None:
        """从磁盘加载字体索引"""
        try:
            if self._font_index_file.exists():
                with open(self._font_index_file, encoding="utf-8") as f:
                    data = json.load(f)
                self._font_index = data.get("index", {})
                logger.info(f"已加载字体索引，共 {len(self._font_index)} 个字体名称条目")
            else:
                logger.info("未找到字体索引文件，将在首次去重时构建")
                self._font_index = {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"加载字体索引失败: {e}，将重新构建")
            self._font_index = {}

    def _save_font_index(self) -> None:
        """将字体索引持久化到磁盘"""
        if not self._font_index_file.parent.exists():
            return
        try:
            with open(self._font_index_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "index": self._font_index,
                        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            logger.debug(f"字体索引已保存，共 {len(self._font_index)} 个条目")
        except OSError as e:
            logger.warning(f"保存字体索引失败: {e}")

    def _get_font_name(self, font_path: Path) -> str | None:
        """
        使用 fonttools 从字体文件中提取 PostScript 名称（nameID=6），
        优先使用 family+subfamily 组合以保证唯一性。
        返回唯一标识字体的名称字符串，失败返回 None。
        """
        try:
            font = TTFont(font_path, fontNumber=0)
            names = font["name"]
            family = None
            subfamily = None
            ps_name = None

            for record in names.names:
                try:
                    text = record.toUnicode()
                except Exception:
                    continue
                if not text:
                    continue
                if record.nameID == 1:  # Font Family
                    family = str(text).strip()
                elif record.nameID == 2:  # Font Subfamily
                    subfamily = str(text).strip()
                elif record.nameID == 6:  # PostScript name
                    ps_name = str(text).strip()

            font.close()
            # 优先使用 family + subfamily 组合
            if family:
                return f"{family} {subfamily}" if subfamily else family
            return ps_name or font_path.stem
        except (TTLibError, OSError, KeyError) as e:
            logger.debug(f"读取字体名称失败 {font_path}: {e}")
            return None

    def build_font_index(self) -> dict[str, list[str]]:
        """
        扫描字体库目录，使用 fonttools 提取每个字体的名称，
        构建 {font_name: [file_path, ...]} 的索引。
        支持增量更新：已有索引中的文件跳过读取。
        """
        if not self._fontpath or not Path(self._fontpath).exists():
            logger.warning("字体库路径不存在，无法构建索引")
            return {}

        font_dir = Path(self._fontpath)
        new_index: dict[str, list[str]] = {}
        indexed_paths: set[str] = set()

        # 收集所有已索引的文件路径
        for paths in self._font_index.values():
            indexed_paths.update(paths)

        # 收集当前磁盘上的实际字体文件
        current_files: list[Path] = []
        for ext in self.FONT_EXTENSIONS:
            current_files.extend(font_dir.rglob(f"*{ext}"))
            current_files.extend(font_dir.rglob(f"*{ext.upper()}"))

        logger.info(f"开始构建字体索引，扫描到 {len(current_files)} 个字体文件")

        for font_file in current_files:
            file_path_str = font_file.as_posix()

            # 如果该文件已在索引中且文件仍存在，复用旧索引中的名称
            if file_path_str in indexed_paths:
                for name, paths in self._font_index.items():
                    if file_path_str in paths:
                        new_index.setdefault(name, []).append(file_path_str)
                        break
                continue

            # 新文件，使用 fonttools 提取名称
            font_name = self._get_font_name(font_file)
            if font_name:
                new_index.setdefault(font_name, []).append(file_path_str)

        # 清理已删除的文件
        valid_count = sum(len(v) for v in new_index.values())
        logger.info(f"字体索引构建完成，共 {len(new_index)} 个字体名称，{valid_count} 个有效文件")
        self._font_index = new_index
        self._save_font_index()
        return new_index

    def _add_to_index(self, font_path: Path) -> None:
        """将新解压的字体文件添加到索引中"""
        font_name = self._get_font_name(font_path)
        if font_name:
            file_path_str = font_path.as_posix()
            self._font_index.setdefault(font_name, []).append(file_path_str)
            self._save_font_index()

    # ------------------------------- 字体去重 ------------------------------- #

    def deduplicate_fonts(self) -> None:
        """
        字体去重后台任务：
        1. 构建/更新字体索引（增量，避免全量扫盘）
        2. 对同名字体保留一份，删除/移动冗余副本
        """
        if not self._fontpath or not Path(self._fontpath).exists():
            logger.warning("字体库路径不存在，跳过去重")
            return

        logger.info("字体去重任务开始")

        # 先构建/更新索引
        self.build_font_index()

        # 找出有重复的字体名称
        duplicates = {name: paths for name, paths in self._font_index.items() if len(paths) > 1}
        if not duplicates:
            logger.info("未发现重复字体，去重任务结束")
            return

        logger.info(f"发现 {len(duplicates)} 组重复字体，开始处理")

        total_removed = 0
        total_kept = 0
        try:
            for font_name, file_paths in duplicates.items():
                kept, removed = self._deduplicate_one(font_name, file_paths)
                total_kept += kept
                total_removed += removed
        except Exception as e:
            logger.error(f"处理去重时出错: {e}")

        logger.info(f"字体去重任务完成：保留 {total_kept} 个，移除 {total_removed} 个")

    def _deduplicate_one(self, font_name: str, file_paths: list[str]) -> tuple[int, int]:
        """
        对同名字体执行去重：按策略保留一个，删除其余。
        返回 (保留数量, 移除数量)。
        """
        # 过滤掉已不存在的文件
        existing: list[Path] = [Path(p) for p in file_paths if os.path.exists(p)]
        if len(existing) <= 1:
            # 清理索引中已不存在的条目
            self._font_index[font_name] = [p.as_posix() for p in existing]
            if not existing:
                self._font_index.pop(font_name, None)
            return len(existing), 0

        # 按策略排序：保留第一个，删除其余
        if self._dedup_keep == "largest":
            existing.sort(key=lambda p: p.stat().st_size, reverse=True)
        else:  # newest
            existing.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        keep = existing[0]
        to_remove = existing[1:]

        logger.info(f"字体 '{font_name}' 共 {len(existing)} 个副本，保留 {keep.name}")
        try:
            for dup_file in to_remove:
                if self._dedup_action == "delete":
                    os.remove(dup_file)
                    logger.info(f"  已删除重复字体: {dup_file.name}")
                else:
                    # move: 移动到备份目录（暂不实现，按 delete 处理）
                    os.remove(dup_file)
                    logger.info(f"  已删除重复字体: {dup_file.name}")
        except OSError as e:
            logger.error(f"删除重复字体失败: {e}")

        # 更新索引
        self._font_index[font_name] = [keep.as_posix()]
        self._save_font_index()
        return 1, len(to_remove)

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
        解压下载完成的 Font 文件，使用 unar 支持 7z/rar/zip 等格式，
        并将新解压的字体文件加入索引。
        """
        font_files: list[str] = [file.name for file in torrent_files if file.id in font_file_ids]
        extract_dir = Path(self._fontpath)
        extract_dir.mkdir(parents=True, exist_ok=True)
        # 记录解压前的字体文件集合，用于增量更新索引
        existing_files = set()
        for ext in self.FONT_EXTENSIONS:
            existing_files.update(extract_dir.rglob(f"*{ext}"))
            existing_files.update(extract_dir.rglob(f"*{ext.upper()}"))

        for font_file_path_str in font_files:
            file_path = Path(save_path) / font_file_path_str
            try:
                subprocess.run(
                    [
                        "unar",
                        "-quiet",
                        "-force-overwrite",
                        "-output-directory",
                        extract_dir.as_posix(),
                        "-no-directory",
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

        # 增量更新字体索引：仅对新文件提取名称
        current_files = set()
        for ext in self.FONT_EXTENSIONS:
            current_files.update(extract_dir.rglob(f"*{ext}"))
            current_files.update(extract_dir.rglob(f"*{ext.upper()}"))
        new_files = current_files - existing_files
        if new_files:
            logger.info(f"检测到 {len(new_files)} 个新字体文件，更新索引...")
            for new_file in new_files:
                self._add_to_index(new_file)
            logger.info("字体索引已更新")

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
