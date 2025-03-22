from abc import ABC, abstractmethod
from asyncio import run
from pathlib import Path
from typing import AsyncGenerator, Set

import aiofiles.os as aio_os
from app.log import logger

from plugins.alist2strm.bloom import CoutingBloomFilter


class Cleaner(ABC):
    """
    抽象基类，定义 Cleaner 的通用接口。
    """

    def __init__(self, need_suffix: list, target_dir: Path) -> None:
        """
        初始化 Cleaner。

        :param need_suffix: 需要处理的文件后缀列表
        :param target_dir: 目标目录
        """
        self.target_dir = target_dir
        self.need_suffix = tuple(need_suffix)

    @abstractmethod
    def add(self, item: Path) -> None:
        """
        添加路径到过滤器中。
        """
        pass

    @abstractmethod
    def remove(self, item: Path) -> None:
        """
        从过滤器中移除路径。
        """
        pass

    @abstractmethod
    def contains(self, item: Path) -> bool:
        """
        检查路径是否存在于过滤器中。

        :return: 如果存在返回 True，否则返回 False
        """
        pass

    @abstractmethod
    async def clean_inviially(self, all_remote_files: Set[Path]) -> None:
        """
        清理无效文件。

        :param all_remote_files: 远程文件集合
        """
        pass

    @staticmethod
    async def _walk_local_files(
        target_dir: Path, need_suffix: list
    ) -> AsyncGenerator[Path, None]:
        """
        遍历本地文件路径。

        :param target_dir: 目标目录
        :param need_suffix: 需要处理的文件后缀列表
        :yield: 文件路径
        """
        for entry in await aio_os.scandir(target_dir):
            if entry.is_file() and (entry.name.endswith(need_suffix)):
                yield Path(entry.path)

    @staticmethod
    async def delete_file(file_path: Path):
        await aio_os.remove(file_path)
        logger.info(f"删除文件：{file_path}")


class SetCleaner(Cleaner):
    """
    使用集合实现的 Cleaner。
    """

    def __init__(self, need_suffix: list, target_dir: Path) -> None:
        super().__init__(need_suffix, target_dir)
        self._filter: Set[Path] = set()
        run(self._init_cleaner())

    def add(self, item: Path) -> None:
        self._filter.add(item)

    def remove(self, item: Path) -> None:
        self._filter.remove(item)

    def contains(self, item: Path) -> bool:
        return item in self._filter

    async def _init_cleaner(self) -> None:
        """
        初始化过滤器，加载本地文件。
        """
        async for path in self._walk_local_files(self.target_dir, self.need_suffix):
            self._filter.add(path)

    async def clean_inviially(self, all_remote_files: Set[Path]) -> None:
        """
        清理无效文件。
        """
        need_delete_files = self._filter - all_remote_files
        for path in need_delete_files:
            await self.delete_file(path)
            self._filter.remove(path)


class IoCleaner(Cleaner):
    """
    基于文件系统 I/O 实现的 Cleaner。
    """

    def __init__(self, need_suffix: list, target_dir: Path) -> None:
        super().__init__(need_suffix, target_dir)

    def add(self, item: Path) -> None:
        pass

    def remove(self, item: Path) -> None:
        pass

    def contains(self, item: Path) -> bool:
        return item.exists()

    async def clean_inviially(self, all_remote_files: Set[Path]) -> None:
        """
        清理无效文件。
        """
        async for path in self._walk_local_files(self.target_dir, self.need_suffix):
            if path not in all_remote_files:
                await self.delete_file(path)


class BloomCleaner(Cleaner):
    """
    基于布隆过滤器实现的 Cleaner。
    """

    def __init__(self, need_suffix: list, target_dir: Path) -> None:
        super().__init__(need_suffix, target_dir)
        self._filter = CoutingBloomFilter()
        run(self._init_cleaner())

    def add(self, item: Path) -> None:
        self._filter.add(str(item))

    def remove(self, item: Path) -> None:
        self._filter.remove(item)

    def contains(self, item: Path) -> bool:
        return str(item) in self._filter

    async def _init_cleaner(self) -> None:
        """
        初始化过滤器，加载本地文件。
        """
        async for path in self._walk_local_files(self.target_dir, self.need_suffix):
            self._filter.add(path)

    async def clean_inviially(self, all_remote_files: Set[Path]) -> None:
        """
        清理无效文件。
        """
        async for path in self._walk_local_files(self.target_dir, self.need_suffix):
            if path not in all_remote_files:
                await self.delete_file(path)
                self._filter.remove(path)
