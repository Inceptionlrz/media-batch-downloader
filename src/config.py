"""配置加载：config.toml 覆盖默认值。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)


@dataclass
class Config:
    output_dir: str = "output"
    concurrency: int = 4
    retries: int = 3
    timeout: int = 30
    proxy: str = ""
    user_agent: str = DEFAULT_UA
    video_format: str = "bv*+ba/b"      # yt-dlp 格式选择器：最高画质视频+最高音质音频
    merge_output: str = "mp4"
    min_height: int = 0                 # 低于该高度视为不合格，触发引擎回退
    download_cover: bool = False
    engine_order: dict[str, list[str]] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)

    def cookie_for(self, platform: str) -> str:
        return self.cookies.get(platform, "")


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    cfg = Config()
    cfg.engine_order = {
        "douyin": ["f2", "ytdlp"],
        "tiktok": ["ytdlp", "f2"],
        "*": ["ytdlp"],
    }
    if not path.exists():
        return cfg

    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    g = data.get("general", {})
    cfg.output_dir = g.get("output_dir", cfg.output_dir)
    cfg.concurrency = int(g.get("concurrency", cfg.concurrency))
    cfg.retries = int(g.get("retries", cfg.retries))
    cfg.timeout = int(g.get("timeout", cfg.timeout))
    cfg.download_cover = bool(g.get("download_cover", cfg.download_cover))

    n = data.get("network", {})
    cfg.proxy = n.get("proxy", cfg.proxy)
    cfg.user_agent = n.get("user_agent", cfg.user_agent)

    q = data.get("quality", {})
    cfg.video_format = q.get("video_format", cfg.video_format)
    cfg.merge_output = q.get("merge_output", cfg.merge_output)
    cfg.min_height = int(q.get("min_height", cfg.min_height))

    for platform in ("douyin", "tiktok", "instagram", "twitter", "*"):
        section = data.get(platform, {})
        if not section:
            continue
        cookie = section.get("cookie", "")
        if cookie:
            cfg.cookies[platform] = cookie
        order = section.get("engines")
        if order:
            cfg.engine_order[platform] = list(order)

    # 环境变量兜底（避免把 cookie 写进文件）
    for plat, var in (("douyin", "DOUYIN_COOKIE"), ("tiktok", "TIKTOK_COOKIE")):
        env = os.environ.get(var, "")
        if env:
            cfg.cookies[plat] = env
    return cfg
