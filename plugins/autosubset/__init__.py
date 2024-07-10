import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, List, Dict, Tuple

from app.utils.system import SystemUtils
from app.core.event import eventmanager, Event
from app.schemas.types import EventType
from app.plugins import _PluginBase
from app.log import logger

class AutoSubset(_PluginBase):
    # 插件名称
    plugin_name = "字幕子集化"
    # 插件描述
    plugin_desc = "转移完成后自动将目录下的字幕子集化"
    # 插件图标
    plugin_icon = "assfonts.png"
    # 插件版本
    plugin_version = "1.0"
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

    # 私有属性
    _enabled = False
    _version = None
    _fontpath = None
    _overwrite = False
    _fontrename = False 
    _deletesubfontfolder = False

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._fontpath = config.get("fontpath")
            self._overwrite = config.get("overwrite")
            self._fontrename = config.get("fontrename")
            self._deletesubfontfolder = config.get("deletesubfontfolder")
            self._af_path = self.get_data_path()
            self._binary_name = 'assfonts'
            if not Path(self._fontpath).exists() or not Path(f'{self._af_path}/{self._binary_name}').exists():
                self._enabled = False
                self.__update_config()
                logger.error(f"未配置字体库或assfonts可执行版本不存在，插件退出")
                return
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'fontpath',
                                            'label': '字体库'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'version',
                                            'readonly': True,
                                            'label': 'assfonts版本',
                                            'placeholder': '暂未安装'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
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
                                            'label': '启用',
                                        }
                                    }
                                ]
                            },
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
                                            'model': 'fontrename',
                                            'label': '字体重命名',
                                        }
                                    }
                                ]
                            },
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
                                            'model': 'overwrite',
                                            'label': '覆盖原文件',
                                        }
                                    }
                                ]
                            },
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
                                            'model': 'deletesubfontfolder',
                                            'label': '删除子集化后的字体',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '请自行下载assfonts二进制文件到插件数据目录'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "version": None,
            "fontpath": None,
            "overwrite":False,
            "fontrename": False,
            "deletesubfontfolder": False
        }
    
    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "version": self.__get_version(),
            "fontpath": self._fontpath,
            "overwrite": self._overwrite,
            "fontrename": self._fontrename,
            "deletesubfontfolder": self._deletesubfontfolder
        })

    def __get_version(self):
        if Path(f'{self._af_path}/{self._binary_name}').exists():
            self._version = SystemUtils.execute(f'cd {self._af_path} && chmod a+x {self._binary_name} && ./{self._binary_name}').split(" ")[1]
        return self._version

    def __overwrite_original_file(self, file: str, assfont_rename: bool):
        if os.path.exists(file):
            os.remove(file)
        if assfont_rename:
            assfont_file = os.path.splitext(file)[0] + ".rename.assfonts.ass"
        else:
            assfont_file = os.path.splitext(file)[0] + ".assfonts.ass"
        if os.path.exists(assfont_file):
            os.rename(assfont_file, file)

    def __delete_subsetfont_folder(self, font_folder_path: str):
        if os.path.exists(font_folder_path):
            shutil.rmtree(font_folder_path)

    def __delete_plain_ass(self, plain_rename_file: str):
        if os.path.exists(plain_rename_file):
            os.remove(plain_rename_file)


    def __assfonts_shell(self, text: str):
        if "[ERROR]" in text:
            error_lines = [line for line in text.splitlines() if "[ERROR]" in line]
            for line in error_lines:
                logger.error(f"检测到错误: {line}")
            if "Missing the font:" in text:
                missing_fonts = re.findall(r'Missing the font: "([^"]+)"', text)
                if missing_fonts:
                    logger.error(f"字体缺失: {','.join(missing_fonts)}")
            return 1
        return 0

    def __build_af_command(self, input_ass: Path) -> List[str]:
        af_command = [
            f"{self._af_path}/{self._binary_name}",
            "-i", str(input_ass),
            "-f", self._fontpath
        ]
        if self._fontrename:
            af_command.append("-r")
        return af_command

    def __subset_ass_file(self, input_ass: Path):
        try:
            af_command = self.__build_af_command(input_ass)
            result = subprocess.run(af_command, stdout=subprocess.PIPE, text=True)

            return self.__assfonts_shell(text=result.stdout)

        except Exception as e:
            logger.error(f"处理文件 {input_ass} 时发生错误: {e}")
            

    @eventmanager.register(EventType.TransferComplete)
    def task_in(self, event: Event):
        #获取要处理的文件路径
        work_file = Path(event.event_data['transferinfo'].target_path)
        # 获取同目录下的ass文件
        ass_list = [ass for ass in work_file.parent.rglob("*.ass") if ".assfonts." not in ass.name]        
        for input_ass in ass_list:
            # 执行命令:
            if self.__subset_ass_file(input_ass=input_ass):
                logger.info(f"{input_ass.name} 子集化失败")
                return
            logger.info(f"{input_ass.name} 子集化成功")

            # 处理完字幕后，如果设置了删除字体文件夹选项，则删除该文件夹
            if self._deletesubfontfolder:
                font_folder_name = input_ass.stem + "_subsetted"
                self.__delete_subsetfont_folder(font_folder_path=os.path.join(str(input_ass.parent), font_folder_name))
                
            # 处理完字幕后，删除纯重名文件
            if self._fontrename:
                plain_rename_file = input_ass.stem + ".rename.ass"
                self.__delete_plain_ass(plain_rename_file=os.path.join(str(input_ass.parent), plain_rename_file))

            if self._overwrite:
                self.__overwrite_original_file(file=str(input_ass), assfont_rename=self._fontrename)

    def stop_service(self):
        """
        退出插件
        """
        pass