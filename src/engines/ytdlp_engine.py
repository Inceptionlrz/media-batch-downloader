"""基于 yt-dlp 的通用引擎（TikTok / Instagram / YouTube / X ...）。"""

from __future__ import annotations

import json
from pathlib import Path

import yt_dlp

from ..config import Config
from ..models import Author, MediaItem
from ..router import platform_of
from ..utils import ensure_dir, netscape_cookiefile
from .base import Engine

# 只接受通用站点；抖音交给 f2 引擎
SKIP_EXTRACTORS = set()


class YtdlpEngine(Engine):
    name = "ytdlp"
    platforms = ("tiktok", "instagram", "youtube", "twitter", "x", "bilibili", "*")

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self._last_dir: Path | None = None

    # ---------- options ----------
    def _opts(self, url: str | None = None, platform: str | None = None,
              outtmpl: str | None = None, playlistend: int | None = None) -> dict:
        platform = platform or (platform_of(url) if url else None)
        cookie = self.cfg.cookie_for(platform) if platform else ""
        if not cookie:
            cookie = self.cfg.cookie_for("tiktok") or self.cfg.cookie_for("douyin")
        headers = {
            "User-Agent": self.cfg.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        opts = {
            "format": self.cfg.video_format,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": False,
            "socket_timeout": self.cfg.timeout,
            "http_headers": headers,
            "retries": self.cfg.retries,
            "fragment_retries": self.cfg.retries,
            "concurrent_fragment_downloads": 4,
            # 只做封装转换，绝不重编码 —— 保证原画质
            "merge_output_format": self.cfg.merge_output,
            "postprocessor_args": {"ffmpeg": ["-c", "copy"]},
            "overwrites": False,
        }
        if self.cfg.proxy:
            opts["proxy"] = self.cfg.proxy
        cookief = netscape_cookiefile(cookie, platform) if platform else None
        if cookief:
            opts["cookiefile"] = str(cookief)
        elif cookie:
            headers["Cookie"] = cookie
        if outtmpl:
            opts["outtmpl"] = outtmpl
        if playlistend:
            opts["playlistend"] = playlistend
        return opts

    # ---------- capability ----------
    def supports(self, url: str, platform: str) -> bool:
        try:
            return bool(yt_dlp.YoutubeDL({"quiet": True}).suitable(url))
        except Exception:
            return False

    # ---------- parse ----------
    @staticmethod
    def _to_item(info: dict) -> MediaItem:
        platform = (info.get("extractor_key") or info.get("ie_key") or "").lower()
        req = info.get("requested_formats") or []
        video_urls, audio_urls = [], []
        if req:
            for f in req:
                if not f.get("url"):
                    continue
                if f.get("vcodec") not in (None, "none"):
                    video_urls.append(f["url"])
                elif f.get("acodec") not in (None, "none"):
                    audio_urls.append(f["url"])
        elif info.get("url"):
            video_urls.append(info["url"])

        images = []
        for img in info.get("images") or []:
            if isinstance(img, dict) and img.get("url"):
                images.append(img["url"])

        item = MediaItem(
            platform=platform or "generic",
            item_id=str(info.get("id") or ""),
            url=info.get("webpage_url") or info.get("original_url") or "",
            desc=info.get("title") or info.get("description") or "",
            author=Author(
                platform=platform,
                uid=str(info.get("uploader_id") or info.get("channel_id") or ""),
                nickname=info.get("uploader") or info.get("channel") or "",
                unique_id=info.get("uploader_url") or "",
            ),
            create_time=int(info.get("timestamp") or 0),
            media_type="image" if images and not video_urls else "video",
            video_urls=video_urls,
            audio_urls=audio_urls,
            image_urls=images,
            cover_url=info.get("thumbnail") or "",
            duration=float(info.get("duration") or 0),
            width=int(info.get("width") or 0),
            height=int(info.get("height") or 0),
            engine="ytdlp",
        )
        item.raw["info"] = {
            k: v for k, v in info.items()
            if k in ("formats", "requested_formats", "url", "ext", "vcodec", "acodec", "protocol")
        }
        return item

    def resolve(self, url: str) -> MediaItem:
        with yt_dlp.YoutubeDL(self._opts(url=url)) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise RuntimeError("播放列表为空")
            info = entries[0]
        return self._to_item(info)

    def profile(self, url: str, limit: int = 0) -> list[MediaItem]:
        with yt_dlp.YoutubeDL(self._opts(url=url, playlistend=limit or None)) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("_type") != "playlist":
            return [self._to_item(info)]
        items = []
        for e in info.get("entries") or []:
            if not e:
                continue
            try:
                items.append(self._to_item(e))
            except Exception:
                continue
            if limit and len(items) >= limit:
                break
        return items

    # ---------- download ----------
    def download(self, item: MediaItem, dest_dir: Path, stem: str) -> list[Path]:
        ensure_dir(dest_dir)
        # 幂等：目标已存在则直接返回，避免重复下载
        exists = sorted(p for p in dest_dir.iterdir()
                        if p.name.startswith(stem) and not p.name.endswith(".part"))
        if exists:
            return exists
        target = dest_dir / f"{stem}.%(ext)s"
        before = {p.name for p in dest_dir.iterdir()}
        url = item.url or item.raw.get("requested_url", "")
        if not url:
            raise RuntimeError("缺少可下载的原始链接")
        opts = self._opts(url=url, platform=item.platform, outtmpl=str(target))
        opts["format"] = f"{self.cfg.video_format}/b"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as exc:
            raise RuntimeError(str(exc)) from exc
        # 按文件名前缀精确归属，避免并发时把其他任务的文件算进来
        new = [p for p in dest_dir.iterdir()
               if p.name not in before and p.name.startswith(stem)
               and not p.name.endswith(".part")]
        return sorted(new)

    @staticmethod
    def formats_report(url: str, cfg: Config) -> str:  # 调试用
        engine = YtdlpEngine(cfg)
        with yt_dlp.YoutubeDL(engine._opts(url=url)) as ydl:
            info = ydl.extract_info(url, download=False)
        lines = []
        for f in info.get("formats") or []:
            lines.append(json.dumps({
                "id": f.get("format_id"), "ext": f.get("ext"),
                "res": f"{f.get('width')}x{f.get('height')}",
                "vcodec": f.get("vcodec"), "acodec": f.get("acodec"),
                "tbr": f.get("tbr"),
            }, ensure_ascii=False))
        return "\n".join(lines)
