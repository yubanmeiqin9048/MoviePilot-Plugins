import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from app.core.config import settings
from app.core.event import eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.system import SystemUtils


class AutoSubset(_PluginBase):
    # 插件名称
    plugin_name = "字幕子集化"
    # 插件描述
    plugin_desc = "转移完成后自动将目录下的字幕子集化"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/yubanmeiqin9048/MoviePilot-Plugins/main/icons/Assfonts.png"
    # 插件版本
    plugin_version = "1.2"
    # 插件作者
    plugin_author = "yubanmeiqin9048"
    # 作者主页
    author_url = "https://github.com/yubanmeiqin9048"
    # 插件配置项ID前缀
    plugin_config_prefix = "autosubset_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._enabled = False

    def init_plugin(self, config: dict | None = None):
        if config:
            self._enabled: bool = config.get("enabled", False)
            self._fontpath: str = config.get("fontpath", "")
            self._overwrite: bool = config.get("overwrite", False)
            self._fontrename: bool = config.get("fontrename", False)
            self._hdrluminance: bool = config.get("hdrluminance", False)
            self._deletesubfontfolder: bool = config.get("deletesubfontfolder", False)
            self._afpath = self.get_data_path()
            self._binaryname = "assfonts"
            if not Path(self._fontpath).exists() or not Path(f"{self._afpath}/{self._binaryname}").exists():
                self._enabled = False
                self.__update_config()
                logger.error("未配置字体库或assfonts可执行版本不存在，插件退出")
                return
            self.__update_config()
            self.__init_assfonts()

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
        return [
            {
                "component": "VForm",
                "content": [
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
                                            "label": "字体库",
                                        },
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
                                            "model": "version",
                                            "readonly": True,
                                            "label": "assfonts版本",
                                            "placeholder": "暂未安装",
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
                                            "model": "enabled",
                                            "label": "启用",
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
                                            "model": "fontrename",
                                            "label": "字体重命名",
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
                                            "model": "overwrite",
                                            "label": "覆盖原文件",
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
                                            "model": "hdrluminance",
                                            "label": "调整字幕HDR亮度",
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
                                            "model": "deletesubfontfolder",
                                            "label": "删除子集化后的字体",
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
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "请自行下载assfonts二进制文件到插件数据目录",
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
            "version": None,
            "fontpath": None,
            "overwrite": False,
            "fontrename": False,
            "hdrluminance": False,
            "deletesubfontfolder": False,
        }

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "version": self.__get_version(),
                "fontpath": self._fontpath,
                "overwrite": self._overwrite,
                "fontrename": self._fontrename,
                "hdrluminance": self._hdrluminance,
                "deletesubfontfolder": self._deletesubfontfolder,
            }
        )

    def __init_assfonts(self):
        os.popen(f"cd {self._afpath} && ./{self._binaryname} -b -f {self._fontpath} -d {self._fontpath}")  # noqa: S605

    def __get_version(self):
        if Path(f"{self._afpath}/{self._binaryname}").exists():
            self._version = SystemUtils.execute(
                f"cd {self._afpath} && chmod a+x {self._binaryname} && ./{self._binaryname}"
            ).split(" ")[1]
        return self._version

    def __process_ass(self, ass_file: Path):
        try:
            cmd = self.__build_af_command(ass_file)
            result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)  # noqa: PLW1510, S603
            if self.__check_errors(result.stdout):
                return False
            if self._deletesubfontfolder:
                shutil.rmtree(ass_file.parent / f"{ass_file.stem}_subsetted", ignore_errors=True)
            if self._fontrename:
                (ass_file.parent / f"{ass_file.stem}.rename.ass").unlink(missing_ok=True)
            if self._overwrite:
                new_file = ass_file.with_name(f"{ass_file.stem}{'.rename' if self._fontrename else ''}.assfonts.ass")
                if new_file.exists():
                    new_file.replace(ass_file)
            return True
        except Exception as e:
            logger.error(f"处理 {ass_file.name} 失败: {e}")
            return False

    def __check_errors(self, text: str):
        if "[ERROR]" in text:
            error_lines = [line for line in text.splitlines() if "[ERROR]" in line]
            for line in error_lines:
                logger.error(f"检测到错误: {line}")
            if "Missing the font:" in text:
                missing_fonts = re.findall(r'Missing the font: "([^"]+)"', text)
                if missing_fonts:
                    logger.error(f"字体缺失: {','.join(missing_fonts)}")
            return False
        return True

    @property
    def mp_version(self) -> Literal["v1", "v2"]:
        return "v2" if hasattr(settings, "VERSION_FLAG") else "v1"

    def __build_af_command(self, input_ass: Path) -> list[str]:
        af_command = [
            f"{self._afpath}/{self._binaryname}",
            "-i",
            str(input_ass),
            "-d",
            self._fontpath,
        ]
        if self._fontrename:
            af_command.append("-r")
        if self._sethdrluminance:
            af_command.append("-l")
        return af_command

    @eventmanager.register(EventType.TransferComplete)
    def task_in(self, event):
        iter_ass: Iterable[Path] = (
            Path(event.event_data["transferinfo"].target_path).parent.glob("**/*.ass")
            if self.mp_version == "v1"
            else [Path(sub) for sub in event.event_data["transferinfo"].subtitle_list_new]
        )
        self._sethdrluminance = "hdr" in event.event_data["meta"].edition and self._hdrluminance
        self.__init_assfonts()
        for ass in iter_ass:
            if ass.suffix != ".ass":
                continue
            if self.__process_ass(ass):
                logger.info(f"{ass.name} 处理成功")
            else:
                logger.warning(f"{ass.name} 处理失败")

    def stop_service(self):
        """
        退出插件
        """
        pass
