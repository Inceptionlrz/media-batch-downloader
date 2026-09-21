"""Cookie 获取：配置 → 浏览器 → 平台 fresh token。"""

from __future__ import annotations

import http.cookiejar

import httpx

from .config import Config

DOMAINS = {
    "douyin": ("douyin.com", ".douyin.com", "www.douyin.com"),
    "tiktok": ("tiktok.com", ".tiktok.com", "www.tiktok.com"),
}


def _from_browser(platform: str) -> str:
    """从本机 Chrome / Edge / Firefox 读取已登录的站点 Cookie。"""
    try:
        import browser_cookie3
    except Exception:
        return ""
    loaders = []
    for name in ("chrome", "edge", "firefox"):
        fn = getattr(browser_cookie3, name, None)
        if fn:
            loaders.append(fn)
    for loader in loaders:
        try:
            jar = loader(domain_name=DOMAINS[platform][0])
        except Exception:
            continue
        parts = [f"{c.name}={c.value}" for c in jar if c.domain and "douyin" in c.domain]
        if platform == "tiktok":
            parts = [f"{c.name}={c.value}" for c in jar if c.domain and "tiktok" in c.domain]
        if parts:
            return "; ".join(parts)
    return ""


def _fresh_douyin(cfg: Config) -> str:
    """生成未登录的 fresh cookie：ttwid + __ac_nonce。"""
    parts: list[str] = []
    try:
        from f2.apps.douyin.utils import TokenManager

        ttwid = TokenManager.gen_ttwid()
        if ttwid:
            parts.append(f"ttwid={ttwid}")
    except Exception:
        pass
    try:
        with httpx.Client(headers={"User-Agent": cfg.user_agent},
                          proxy=cfg.proxy or None, timeout=cfg.timeout,
                          follow_redirects=True) as cli:
            cli.get("https://www.douyin.com/")
            for name, value in cli.cookies.items():
                parts.append(f"{name}={value}")
    except Exception:
        pass
    return "; ".join(parts)


def _fresh_tiktok(cfg: Config) -> str:
    parts: list[str] = []
    try:
        with httpx.Client(headers={"User-Agent": cfg.user_agent},
                          proxy=cfg.proxy or None, timeout=cfg.timeout,
                          follow_redirects=True) as cli:
            cli.get("https://www.tiktok.com/")
            for name, value in cli.cookies.items():
                parts.append(f"{name}={value}")
    except Exception:
        pass
    return "; ".join(parts)


def export_cookie_to_config(platform: str, cookie: str, path: Path) -> bool:
    """把 cookie 写入 config.toml（不存在则基于模板生成）。"""
    import re
    from .config import ROOT

    target = path or ROOT / "config.toml"
    if not target.exists():
        tmpl = ROOT / "config.example.toml"
        if tmpl.exists():
            target.write_text(tmpl.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            target.write_text(f"[{platform}]\ncookie = \"{cookie}\"\n", encoding="utf-8")
            return True

    text = target.read_text(encoding="utf-8")
    # 在目标平台段内替换 cookie 行
    pattern = re.compile(r"(\[" + platform + r"\][\s\S]*?cookie\s*=\s*)\"[^\"]*\"")
    if pattern.search(text):
        text = pattern.sub(lambda m: m.group(1) + f'"{cookie}"', text, count=1)
    else:
        text += f'\n[{platform}]\ncookie = "{cookie}"\n'
    target.write_text(text, encoding="utf-8")
    return True


def ensure_cookies(cfg: Config, platforms: tuple[str, ...] = ("douyin", "tiktok"),
                   allow_browser: bool = True, verbose: bool = True) -> None:
    """按优先级补齐 cookie，写回 cfg.cookies。"""
    for plat in platforms:
        if cfg.cookie_for(plat):
            continue
        cookie = ""
        if allow_browser:
            cookie = _from_browser(plat)
            if cookie and verbose:
                print(f"[cookie] {plat}: 已从浏览器读取")
        if not cookie:
            cookie = _fresh_douyin(cfg) if plat == "douyin" else _fresh_tiktok(cfg)
            if cookie and verbose:
                print(f"[cookie] {plat}: 已生成未登录 fresh cookie")
        if cookie:
            cfg.cookies[plat] = cookie
        elif verbose:
            print(f"[cookie] {plat}: 未获取到 cookie（可在 config.toml 手动配置）")
