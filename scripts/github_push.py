"""用 GitHub Git Data API 批量上传文件（git push 在本机卡死时的备用通道）。

用法：python scripts/github_push.py "提交信息"
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

OWNER = "Inceptionlrz"
REPO = "media-batch-downloader"
ROOT = pathlib.Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {".git", ".workbuddy", "output", "logs", "__pycache__", ".venv", "venv"}
EXCLUDE_FILES = {"config.toml", ".dl_index.json"}
EXCLUDE_SUFFIX = {".log", ".pyc", ".pyo"}
EXCLUDE_PREFIX = {".wbapp_"}

API = "https://api.github.com"


def token() -> str:
    return (pathlib.Path.home() / ".workbuddy" / "github_token.txt").read_text().strip()


def request(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"token {token()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "media-batch-downloader",
            "Content-Type": "application/json",
        },
    )
    last = ""
    for proxy in (None, "http://127.0.0.1:7897"):  # 直连被掐 TLS 时回退到 Clash
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
            raw = opener.open(req, timeout=90).read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            return {"__error": exc.code, "__msg": exc.read().decode("utf-8", "ignore")[:400]}
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
    return {"__error": 0, "__msg": f"网络不可达（已试直连与代理）：{last}"}


def collect_files() -> list[tuple[str, bytes]]:
    out = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        parts = rel.parts
        if any(p in EXCLUDE_DIRS for p in parts):
            continue
        if rel.name in EXCLUDE_FILES or path.suffix in EXCLUDE_SUFFIX:
            continue
        if rel.name.startswith(tuple(EXCLUDE_PREFIX)):
            continue
        out.append((rel.as_posix(), path.read_bytes()))
    return out


def ensure_initialized() -> None:
    """空仓库无法创建 blob，先用 Contents API 落一个初始文件。"""
    res = request("GET", f"/repos/{OWNER}/{REPO}/contents/README.md")
    if "__error" not in res:
        return
    init = request("PUT", f"/repos/{OWNER}/{REPO}/contents/README.md",
                   {"message": "chore: initialize repository",
                    "content": base64.b64encode(b"# initializing\n").decode()})
    if "__error" in init:
        print("初始化失败:", init["__error"], init["__msg"])
        raise SystemExit(1)
    print("仓库已初始化")


def main() -> int:
    message = sys.argv[1] if len(sys.argv) > 1 else "update"
    ensure_initialized()
    files = collect_files()
    print(f"待上传 {len(files)} 个文件：")
    for name, _ in files:
        print("  ", name)

    tree = []
    for name, data in files:
        res = request("POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                      {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        if "__error" in res:
            print(f"blob 失败 {name}: {res['__error']} {res['__msg']}")
            return 1
        tree.append({"path": name, "mode": "100644", "type": "blob", "sha": res["sha"]})
    print(f"已创建 {len(tree)} 个 blob")

    res = request("POST", f"/repos/{OWNER}/{REPO}/git/trees", {"tree": tree})
    if "__error" in res:
        print("tree 失败:", res["__error"], res["__msg"])
        return 1
    tree_sha = res["sha"]

    # 空仓库没有父提交
    ref = request("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/main")
    parents = [ref["object"]["sha"]] if "object" in ref else []

    res = request("POST", f"/repos/{OWNER}/{REPO}/git/commits",
                  {"message": message, "tree": tree_sha, "parents": parents})
    if "__error" in res:
        print("commit 失败:", res["__error"], res["__msg"])
        return 1
    commit_sha = res["sha"]
    print("commit:", commit_sha[:8])

    if parents:
        res = request("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/main", {"sha": commit_sha})
    else:
        res = request("POST", f"/repos/{OWNER}/{REPO}/git/refs",
                      {"ref": "refs/heads/main", "sha": commit_sha})
    if "__error" in res:
        print("ref 更新失败:", res["__error"], res["__msg"])
        return 1

    print(f"完成：https://github.com/{OWNER}/{REPO}/commit/{commit_sha[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
