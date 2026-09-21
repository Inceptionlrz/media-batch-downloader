"""链接识别：平台判定 + 短链还原 + 单作品/主页分类。"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

DOUYIN_HOSTS = {"douyin.com", "www.douyin.com", "iesdouyin.com", "www.iesdouyin.com",
                "v.douyin.com", "www.v.douyin.com"}
TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
                "m.tiktok.com"}

HOST_PLATFORM = {
    **{h: "douyin" for h in DOUYIN_HOSTS},
    **{h: "tiktok" for h in TIKTOK_HOSTS},
    "instagram.com": "instagram", "www.instagram.com": "instagram",
    "twitter.com": "twitter", "x.com": "twitter", "www.twitter.com": "twitter",
    "youtube.com": "youtube", "www.youtube.com": "youtube", "youtu.be": "youtube",
    "bilibili.com": "bilibili", "www.bilibili.com": "bilibili",
}


def platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return HOST_PLATFORM.get(host, "generic")


def _is_profile_path(host_kind: str, path: str) -> bool:
    path = path.strip("/")
    if host_kind == "douyin":
        # /user/MS4wLj...  或 /user/self?  ；/video/<id> 是单作品
        return bool(re.match(r"^user/[^/]+/?$", path))
    if host_kind == "tiktok":
        # /@user            → 主页
        # /@user/video/<id> → 单作品
        return bool(re.match(r"^@[^/]+/?$", path))
    if host_kind == "instagram":
        return bool(re.match(r"^[^/]+/?$", path)) and not path.startswith(("p/", "reel/", "reels/"))
    if host_kind == "youtube":
        return path.startswith(("@", "channel/", "c/", "user/"))
    return False


def kind_of(url: str, platform: str) -> str:
    """返回 'post' 或 'profile'。"""
    parsed = urlparse(url)
    path = parsed.path or ""
    if platform == "douyin":
        if re.search(r"/video/\d+", path) or re.search(r"/note/\d+", path):
            return "post"
        if re.match(r"^/user/", path):
            return "profile"
        return "unknown"
    if platform == "tiktok":
        if re.search(r"/video/\d+", path) or re.search(r"/photo/\d+", path):
            return "post"
        if re.match(r"^/@[^/]+/?$", path):
            return "profile"
        return "unknown"
    if _is_profile_path(platform, path):
        return "profile"
    if re.search(r"/(video|photo|p|reel|reels|watch|status)/", path):
        return "post"
    return "unknown"


def resolve_short(url: str, cfg, timeout: int = 20) -> str:
    """展开 v.douyin.com / vm.tiktok.com 等短链为最终 URL。"""
    if urlparse(url).hostname.lower() not in {"v.douyin.com", "vm.tiktok.com", "vt.tiktok.com"}:
        return url
    headers = {"User-Agent": cfg.user_agent}
    try:
        with httpx.Client(headers=headers, proxy=cfg.proxy or None,
                          timeout=timeout, follow_redirects=True) as cli:
            resp = cli.get(url)
            final = str(resp.url)
            return final if final and final != url else url
    except Exception:
        return url


def classify(url: str, cfg) -> tuple[str, str, str]:
    """返回 (final_url, platform, kind)。"""
    final = resolve_short(url, cfg)
    platform = platform_of(final)
    return final, platform, kind_of(final, platform)


def extract_douyin_aweme_id(url: str) -> str:
    m = re.search(r"/(?:video|note|slides)/(\d+)", url)
    return m.group(1) if m else ""


def extract_douyin_sec_uid(url: str) -> str:
    m = re.search(r"/user/([A-Za-z0-9_\-]+)", url)
    if m and not m.group(1).startswith("self"):
        return m.group(1)
    return ""


def extract_tiktok_handle(url: str) -> str:
    m = re.search(r"tiktok\.com/@([A-Za-z0-9_.\-]+)", url)
    return m.group(1) if m else ""
