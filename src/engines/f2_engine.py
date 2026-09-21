"""基于 f2 的抖音 / TikTok 专用引擎（直连平台接口，取无水印原画）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import Config
from ..models import Author, MediaItem
from ..router import (
    extract_douyin_aweme_id,
    extract_douyin_sec_uid,
    extract_tiktok_handle,
)
from ..utils import client, ensure_dir, fetch_bytes, merge_av, stream_download
from .base import Engine


class F2Engine(Engine):
    name = "f2"
    platforms = ("douyin", "tiktok")

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self._cli = None

    # ---------- f2 kwargs ----------
    def _kwargs(self, platform: str) -> dict:
        return {
            "headers": {
                "User-Agent": self.cfg.user_agent,
                "Referer": "https://www.douyin.com/" if platform == "douyin"
                else "https://www.tiktok.com/",
            },
            "proxies": {"http://": self.cfg.proxy or None,
                        "https://": self.cfg.proxy or None},
            "timeout": self.cfg.timeout,
            "cookie": self.cfg.cookie_for(platform),
        }

    def supports(self, url: str, platform: str) -> bool:
        return platform in self.platforms

    # ---------- parsing ----------
    @staticmethod
    def _best_video_url(video: dict) -> tuple[list[str], int, int]:
        """从 bit_rate / play_addr 中挑最高画质且无水印的直链。"""
        best: tuple[int, list[str], int, int] | None = None
        for cand in video.get("bit_rate") or []:
            addr = cand.get("play_addr") or {}
            urls = [u for u in (addr.get("url_list") or []) if u]
            if not urls:
                continue
            # 跳过明确带水印的档位
            if cand.get("is_bytevc1") and not urls:
                continue
            h = int(addr.get("height") or cand.get("height") or 0)
            w = int(addr.get("width") or cand.get("width") or 0)
            score = h * 10_000 + w
            if best is None or score > best[0]:
                best = (score, urls, w, h)

        if best:
            return best[1], best[2], best[3]

        addr = video.get("play_addr") or {}
        urls = [u for u in (addr.get("url_list") or []) if u]
        if not urls:
            daddr = video.get("download_addr") or {}
            urls = [u for u in (daddr.get("url_list") or []) if u]
        return urls, int(addr.get("width") or 0), int(addr.get("height") or 0)

    @classmethod
    def _parse_aweme(cls, aweme: dict, platform: str) -> MediaItem:
        author_raw = aweme.get("author") or {}
        video = aweme.get("video") or {}
        v_urls, w, h = cls._best_video_url(video) if video else ([], 0, 0)

        images: list[str] = []
        for img in aweme.get("images") or []:
            # url_list 末位通常是最高清原图
            urls = [u for u in (img.get("url_list") or []) if u]
            urls = urls or [u for u in (img.get("download_url_list") or []) if u]
            if urls:
                images.append(urls[-1] if len(urls) > 1 else urls[0])

        cover = ((video.get("origin_cover") or {}).get("url_list") or [""])[0] if video else ""
        duration = 0.0
        if video:
            duration = float(video.get("duration") or 0) / 1000.0 or float(aweme.get("duration") or 0)

        aweme_id = str(aweme.get("aweme_id") or aweme.get("id") or "")
        return MediaItem(
            platform=platform,
            item_id=aweme_id,
            url=f"https://www.douyin.com/video/{aweme_id}" if platform == "douyin"
            else f"https://www.tiktok.com/@{author_raw.get('unique_id') or ''}/video/{aweme_id}",
            desc=aweme.get("desc") or "",
            author=Author(
                platform=platform,
                uid=str(author_raw.get("uid") or ""),
                sec_uid=author_raw.get("sec_uid") or "",
                nickname=author_raw.get("nickname") or "",
                unique_id=author_raw.get("unique_id") or author_raw.get("short_id") or "",
            ),
            create_time=int(aweme.get("create_time") or 0),
            media_type="image" if images and not v_urls else "video",
            video_urls=v_urls,
            image_urls=images,
            cover_url=cover,
            duration=duration,
            width=w or int((video.get("play_addr") or {}).get("width") or 0),
            height=h or int((video.get("play_addr") or {}).get("height") or 0),
            raw={"aweme": aweme},
            engine="f2",
        )

    # ---------- resolve ----------
    async def _resolve_douyin(self, url: str) -> MediaItem:
        from f2.apps.douyin.handler import DouyinHandler

        aweme_id = extract_douyin_aweme_id(url)
        if not aweme_id:
            raise RuntimeError(f"无法从链接提取抖音作品 ID: {url}")
        result = await DouyinHandler(self._kwargs("douyin")).fetch_one_video(aweme_id)
        data = self._unwrap(result)
        return self._parse_aweme(data, "douyin")

    async def _resolve_tiktok(self, url: str) -> MediaItem:
        from f2.apps.tiktok.handler import TikTokHandler

        aweme_id = extract_douyin_aweme_id(url)  # /video/<id> 结构通用
        if not aweme_id:
            raise RuntimeError(f"无法从链接提取 TikTok 作品 ID: {url}")
        result = await TikTokHandler(self._kwargs("tiktok")).fetch_one_video(aweme_id)
        data = self._unwrap(result)
        return self._parse_aweme(data, "tiktok")

    @staticmethod
    def _unwrap(result) -> dict:
        for method in ("_to_dict", "_to_raw"):
            fn = getattr(result, method, None)
            if not fn:
                continue
            try:
                data = fn()
            except Exception:
                continue
            if isinstance(data, dict):
                if "aweme_detail" in data:
                    return data["aweme_detail"]
                if "aweme_list" in data and data["aweme_list"]:
                    return data["aweme_list"][0]
                return data
        raise RuntimeError("f2 返回数据无法解析")

    def resolve(self, url: str) -> MediaItem:
        platform = "douyin" if "douyin" in url else "tiktok"
        coro = self._resolve_douyin(url) if platform == "douyin" else self._resolve_tiktok(url)
        return asyncio.run(coro)

    # ---------- profile ----------
    async def _profile_douyin(self, url: str, limit: int) -> list[MediaItem]:
        from f2.apps.douyin.handler import DouyinHandler

        sec_uid = extract_douyin_sec_uid(url)
        if not sec_uid:
            raise RuntimeError(f"无法从主页链接提取 sec_user_id: {url}")
        handler = DouyinHandler(self._kwargs("douyin"))
        items: list[MediaItem] = []
        async for batch in handler.fetch_user_post_videos(sec_uid, 0, 0, 20, limit or None):
            raw = batch._to_raw()
            awemes = raw.get("aweme_list") if isinstance(raw, dict) else raw
            for a in awemes or []:
                try:
                    items.append(self._parse_aweme(a, "douyin"))
                except Exception:
                    continue
                if limit and len(items) >= limit:
                    return items
        return items

    async def _profile_tiktok(self, url: str, limit: int) -> list[MediaItem]:
        from f2.apps.tiktok.handler import TikTokHandler

        handle = extract_tiktok_handle(url)
        if not handle:
            raise RuntimeError(f"无法从主页链接提取 TikTok 用户名: {url}")
        handler = TikTokHandler(self._kwargs("tiktok"))
        items: list[MediaItem] = []
        async for batch in handler.fetch_user_post_videos(handle, 0, 20, limit or None):
            raw = batch._to_raw()
            awemes = raw.get("aweme_list") if isinstance(raw, dict) else raw
            for a in awemes or []:
                try:
                    items.append(self._parse_aweme(a, "tiktok"))
                except Exception:
                    continue
                if limit and len(items) >= limit:
                    return items
        return items

    def profile(self, url: str, limit: int = 0) -> list[MediaItem]:
        if "douyin" in url:
            return asyncio.run(self._profile_douyin(url, limit))
        return asyncio.run(self._profile_tiktok(url, limit))

    # ---------- download ----------
    def download(self, item: MediaItem, dest_dir: Path, stem: str) -> list[Path]:
        ensure_dir(dest_dir)
        if self._cli is None:
            self._cli = client(self.cfg)
        ref = ("https://www.douyin.com/" if item.platform == "douyin"
               else "https://www.tiktok.com/")
        out: list[Path] = []

        for i, url in enumerate(item.video_urls):
            suffix = "" if len(item.video_urls) == 1 else f"_{i + 1}"
            dest = dest_dir / f"{stem}{suffix}.mp4"
            if dest.exists() and dest.stat().st_size > 0:
                out.append(dest)
                continue
            stream_download(self._cli, url, dest, referer=ref)
            out.append(dest)

        for i, url in enumerate(item.image_urls, start=1):
            dest = dest_dir / f"{stem}_{i:02d}.jpg"
            if dest.exists() and dest.stat().st_size > 0:
                out.append(dest)
                continue
            dest.write_bytes(fetch_bytes(self._cli, url, referer=ref))
            out.append(dest)

        return out

    def close(self) -> None:
        if self._cli is not None:
            self._cli.close()
            self._cli = None

    @staticmethod
    def _merge(video: Path, audio: Path, out: Path) -> Path:  # 供分离流场景使用
        return merge_av(video, audio, out)
