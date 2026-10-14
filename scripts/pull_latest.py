#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 GitHub Release 拉取最新一期「监控更新」打包视频到本地 output/。

用法:
    python scripts/pull_latest.py            # 增量拉取最新一期（仅当 tag 比上次新才下）
    python scripts/pull_latest.py --force    # 忽略本地记录，强制重新拉取
    python scripts/pull_latest.py --dry      # 只打印将要拉取的内容，不下载

说明:
- 只认 watch-<N> 系列的 Release（监控更新批次），忽略 batch-1 等历史包。
- 记录上次拉取的 tag 到 output/.last_pull.json，实现增量，避免重复下载。
- 解压保留原始目录结构（output/tiktok/<作者>/...）。
- 代理：优先用环境变量 HTTPS_PROXY/HTTP_PROXY，否则尝试本机 Clash 127.0.0.1:7897。
- 仅用标准库，无第三方依赖。
"""
import json
import os
import sys
import zipfile
from pathlib import Path
import urllib.request
import urllib.error

OWNER = "Inceptionlrz"
REPO = "media-batch-downloader"
API = "https://api.github.com"
OUT = Path(__file__).resolve().parent.parent / "output"
STATE = OUT / ".last_pull.json"
CTX = None  # 默认证书校验


def pick_proxy():
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k)
        if v:
            return v
    return "http://127.0.0.1:7897"  # 本机 Clash 兜底


def _headers():
    h = {"Accept": "application/vnd.github+json",
         "User-Agent": "pull-latest/1.0"}
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        p = Path.home() / ".workbuddy" / "github_token.txt"
        if p.exists():
            tok = p.read_text(encoding="utf-8", errors="ignore").strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _fetch(url, timeout=20):
    """优先 urllib（标准库），失败回退 curl。本机 Clash 代理下 urllib 的
    CONNECT 隧道会触发 SSL UNEXPECTED_EOF，curl 则正常。"""
    import subprocess
    # 1) 尝试 urllib
    try:
        proxy = pick_proxy()
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
        req = urllib.request.Request(url, headers=_headers())
        with opener.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8")
    except Exception:
        pass
    # 2) 回退 curl（显式走代理，避免直连 github TLS 被掐）
    proxy = pick_proxy()
    cmd = ["curl", "-fsSL", "-x", proxy, "--max-time", str(timeout), url]
    tok = _headers().get("Authorization")
    if tok:
        cmd += ["-H", f"Authorization: {tok}"]
    cmd += ["-H", "Accept: application/vnd.github+json"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:200])
    return out.stdout


def http_json(url):
    return json.loads(_fetch(url))


def download_file(url, dest, timeout=180):
    """用 curl 直接流式下载到文件（避免大文件占内存）。
    注意：release 资源的 browser_download_url 会 302 跳转到
    objects.githubusercontent.com，该 CDN 在本机代理下 TLS 握手会失败；
    因此这里统一走 api.github.com 的 assets API（带 Accept:
    application/octet-stream），绕过 CDN 直连。"""
    import subprocess
    cmd = ["curl", "-fsSL", "-x", pick_proxy(), "--max-time", str(timeout),
           "-H", "Accept: application/octet-stream", "-o", str(dest), url]
    tok = _headers().get("Authorization")
    if tok:
        cmd += ["-H", f"Authorization: {tok}"]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError("curl 下载失败（返回码 %d）" % r.returncode)


def latest_watch_release():
    data = http_json(f"{API}/repos/{OWNER}/{REPO}/releases?per_page=20")
    watches = [x for x in data if x.get("tag_name", "").startswith("watch-")]
    if not watches:
        return None
    watches.sort(key=lambda x: int(x["tag_name"].split("-")[1]), reverse=True)
    return watches[0]


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8")).get("tag")
        except Exception:
            return None
    return None


def save_state(tag):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"tag": tag}, ensure_ascii=False, indent=2),
                     encoding="utf-8")


def main():
    force = "--force" in sys.argv
    dry = "--dry" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)

    rel = latest_watch_release()
    if not rel:
        print("未找到任何 watch-* 发布批次。")
        return

    tag = rel["tag_name"]
    last = load_state()
    print(f"最新批次：{tag}  发布于 {rel.get('published_at')}")
    if not force and last == tag:
        print(f"本地已是最新（上次拉取 {last}），无需下载。")
        return

    asset = next((a for a in rel.get("assets", []) if a["name"].endswith(".zip")), None)
    if not asset:
        print("该批次没有 zip 资源。")
        return
    dl = asset["browser_download_url"]
    api_url = asset.get("url")  # api.github.com 的 assets API，绕过 CDN
    size_mb = asset["size"] / 1024 / 1024
    print(f"资源：{asset['name']} ({size_mb:.2f} MB)")

    if dry:
        print("[dry] 仅预览，不下载。")
        return

    tmp = OUT / "_pull_tmp.zip"
    print("下载中…")
    download_file(api_url or dl, tmp, timeout=180)

    print(f"解压到 {OUT} …")
    with zipfile.ZipFile(tmp) as z:
        z.extractall(OUT)
    tmp.unlink()
    save_state(tag)
    print(f"✅ 已拉取 {tag} 并解压完成。本地 output/ 已更新。")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ 拉取失败：{type(e).__name__} {e}")
        sys.exit(1)
