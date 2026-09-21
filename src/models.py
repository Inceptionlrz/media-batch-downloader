"""统一数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Author:
    platform: str = ""
    uid: str = ""
    sec_uid: str = ""
    nickname: str = ""
    unique_id: str = ""

    @property
    def display(self) -> str:
        return self.nickname or self.unique_id or self.sec_uid or self.uid or "unknown"


@dataclass
class MediaItem:
    """一个作品（视频 / 图集）的标准化描述。"""

    platform: str
    item_id: str
    url: str = ""
    desc: str = ""
    author: Author = field(default_factory=Author)
    create_time: int = 0
    media_type: str = "video"  # video | image | album
    video_urls: list[str] = field(default_factory=list)
    audio_urls: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    cover_url: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    engine: str = ""

    @property
    def is_video(self) -> bool:
        return bool(self.video_urls)

    @property
    def is_album(self) -> bool:
        return bool(self.image_urls)

    @property
    def quality_label(self) -> str:
        if self.height:
            return f"{self.width}x{self.height}" if self.width else f"{self.height}p"
        return "-"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.platform}:{self.item_id} {self.media_type} {self.quality_label}>"
