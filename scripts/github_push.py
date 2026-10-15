"""用 GitHub Git Data API 批量上传文件（git push 在本机卡死时的备用通道）。

用法：
  python scripts/github_push.py "提交信息"               # 全量快照（覆盖远端为本地工作区）
  python scripts/github_push.py --delta "提交信息"       # 只提交本地相对远端 head 的改动（推荐）
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

OWNER = "Inceptionlrz"
REPO = "media-batch-downloader"
ROOT = pathlib.Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {".git", ".workbuddy", "output", "logs", "__pycache__", ".venv", "venv",
                "node_modules", ".vercel"}
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


def git_out(*args: str) -> str:
    # -c core.quotepath=false：否则中文路径会被转义成 \xxx，导致读不到文件
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=str(ROOT), capture_output=True, text=True,
    ).stdout.strip()


def collect_delta(base_ref: str = "") -> tuple[str | None, list[tuple[str, bytes]], list[str]]:
    """相对某个基线的增量改动，返回 (远端 base_sha|None, [(路径, 内容)], [删除路径])。

    base_ref 为空时用远端 main 的 head；若该 commit 本地不存在（用 API 提过之后常发生），
    需要显式传 --base=<本地存在的 ref>。
    """
    ref = request("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/main")
    if "object" not in ref:
        raise SystemExit(f"读取远端 main 失败：{ref}")
    remote_sha = ref["object"]["sha"]
    base_ref = base_ref or remote_sha
    status = git_out("diff", "--name-status", base_ref, "HEAD")
    if not status and base_ref == remote_sha:
        raise SystemExit(
            f"diff 为空：远端 head {remote_sha[:8]} 在本地不存在（用 API 提交过？），"
            f"请显式指定 --base=<本地存在的 ref>"
        )
    added, deleted = [], []
    for line in status.splitlines():
        if not line.strip():
            continue
        code, _, path = line.partition("\t")
        path = path.strip()
        if code.startswith("D"):
            deleted.append(path)
        else:
            p = ROOT / path
            if p.is_file():
                added.append((path, p.read_bytes()))
    return remote_sha, added, deleted


def main() -> int:
    args = sys.argv[1:]
    delta = "--delta" in args
    if delta:
        args.remove("--delta")
    message = args[0] if args else "update"

    if delta:
        base_ref = ""
        for a in list(args):
            if a.startswith("--base="):
                base_ref = a.split("=", 1)[1]
                args.remove(a)
        base_sha, files, deleted = collect_delta(base_ref)
        if not files and not deleted:
            print("没有相对远端 head 的改动，跳过")
            return 0
        print(f"增量提交（远端基线 {base_sha[:8]}），{len(files)} 个文件：")
        for name, _ in files:
            print("  + ", name)
        for name in deleted:
            print("  - ", name)

        base_commit = request("GET", f"/repos/{OWNER}/{REPO}/git/commits/{base_sha}")
        base_tree = base_commit.get("tree", {}).get("sha")
        tree = []
        for name, data in files:
            res = request("POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                          {"content": base64.b64encode(data).decode(), "encoding": "base64"})
            if "__error" in res:
                print(f"blob 失败 {name}: {res['__error']} {res['__msg']}")
                return 1
            tree.append({"path": name, "mode": "100644", "type": "blob", "sha": res["sha"]})
        for name in deleted:
            tree.append({"path": name, "mode": "100644", "type": "blob", "sha": None})
        res = request("POST", f"/repos/{OWNER}/{REPO}/git/trees", {"base_tree": base_tree, "tree": tree})
        if "__error" in res:
            print("tree 失败:", res["__error"], res["__msg"])
            return 1
        tree_sha = res["sha"]
        res = request("POST", f"/repos/{OWNER}/{REPO}/git/commits",
                      {"message": message, "tree": tree_sha, "parents": [base_sha]})
        if "__error" in res:
            print("commit 失败:", res["__error"], res["__msg"])
            return 1
        commit_sha = res["sha"]
        res = request("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/main", {"sha": commit_sha})
        if "__error" in res:
            print("ref 更新失败:", res["__error"], res["__msg"])
            return 1
        print(f"完成：https://github.com/{OWNER}/{REPO}/commit/{commit_sha[:8]}")
        return 0

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
