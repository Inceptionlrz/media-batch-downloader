"""引擎抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import MediaItem


class Engine(ABC):
    name = "base"
    platforms: tuple[str, ...] = ()

    def __init__(self, cfg):
        self.cfg = cfg

    @abstractmethod
    def supports(self, url: str, platform: str) -> bool:
        """该引擎是否能处理这个链接。"""

    @abstractmethod
    def resolve(self, url: str) -> MediaItem:
        """解析单个作品链接，返回元信息 + 原画质直链。"""

    @abstractmethod
    def profile(self, url: str, limit: int = 0) -> list[MediaItem]:
        """解析博主主页，返回作品列表。"""

    @abstractmethod
    def download(self, item: MediaItem, dest_dir: Path, stem: str) -> list[Path]:
        """把 item 的媒体落到 dest_dir，返回文件路径列表。"""

    def close(self) -> None:  # pragma: no cover
        pass
