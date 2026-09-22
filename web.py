"""Web 界面服务：FastAPI + 后台任务执行。

启动：python web.py [--host 127.0.0.1] [--port 8770]
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from src.config import load_config  # noqa: E402
from src.cookies import _from_browser, ensure_cookies, export_cookie_to_config  # noqa: E402
from src.engines.f2_engine import F2Engine  # noqa: E402
from src.engines.ytdlp_engine import YtdlpEngine  # noqa: E402
from src.orchestrator import Orchestrator  # noqa: E402
from src.router import classify  # noqa: E402

STATIC = ROOT / "static"
MAX_LOGS = 500

app = FastAPI(title="短视频批量下载器", version="1.0.0")
_lock = threading.Lock()
_tasks: dict[str, "Task"] = {}


# ---------------- models ----------------
class JobRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)
    limit: int = 0
    concurrency: int | None = None
    engine: str = ""
    proxy: str = ""
    output: str = ""
    browser_cookie: bool = False


class Task:
    def __init__(self, req: JobRequest):
        self.id = uuid.uuid4().hex[:10]
        self.req = req
        self.status = "queued"          # queued | running | done | error | cancelled
        self.created = time.time()
        self.finished: float | None = None
        self.logs: list[dict] = []
        self.items: list[dict] = []
        self.stats = {"parsed": 0, "todo": 0, "done": 0, "ok": 0, "fail": 0, "skipped": 0}
        self.error = ""

    def add_log(self, msg: str, level: str = "info") -> None:
        entry = {"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg}
        with _lock:
            self.logs.append(entry)
            if len(self.logs) > MAX_LOGS:
                del self.logs[: len(self.logs) - MAX_LOGS]

    def on_event(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "log":
            self.add_log(ev.get("msg", ""), ev.get("level", "info"))
        elif kind == "phase":
            self.stats.update({"parsed": ev.get("parsed", 0), "todo": ev.get("total", 0),
                               "skipped": ev.get("skipped", 0)})
        elif kind == "item":
            self.items.append(ev)
            self.stats["done"] = ev.get("done", self.stats["done"] + 1)
            self.stats["ok"] += 1 if ev.get("status") == "ok" else 0
            self.stats["fail"] += 1 if ev.get("status") == "fail" else 0
        elif kind == "done":
            self.stats["ok"] = ev.get("ok", self.stats["ok"])
            self.stats["fail"] = ev.get("fail", self.stats["fail"])
            self.stats["skipped"] = ev.get("skipped", self.stats["skipped"])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "created": self.created,
            "finished": self.finished,
            "urls": self.req.urls,
            "stats": self.stats,
            "error": self.error,
            "logs": self.logs[-200:],
            "items": self.items[-300:],
        }


# ---------------- engine / config ----------------
def build_engines(cfg):
    engines = {}
    try:
        engines["ytdlp"] = YtdlpEngine(cfg)
    except Exception as exc:
        print(f"[警告] yt-dlp 引擎不可用: {exc}")
    try:
        engines["f2"] = F2Engine(cfg)
    except Exception as exc:
        print(f"[警告] f2 引擎不可用: {exc}")
    return engines


def make_cfg(req: JobRequest):
    cfg = load_config()
    if req.output:
        cfg.output_dir = req.output
    if req.proxy:
        cfg.proxy = req.proxy
    if req.concurrency:
        cfg.concurrency = req.concurrency
    if req.engine:
        for plat in list(cfg.engine_order):
            cfg.engine_order[plat] = [req.engine]
    ensure_cookies(cfg, allow_browser=req.browser_cookie, verbose=False)
    return cfg


# ---------------- worker ----------------
def run_task(task: Task) -> None:
    task.status = "running"
    try:
        cfg = make_cfg(task.req)
        engines = build_engines(cfg)
        if not engines:
            raise RuntimeError("无可用引擎，请检查 requirements.txt 是否安装完整")

        def on_event(ev: dict):
            task.on_event(ev)

        orch = Orchestrator(cfg, engines, verbose=False, on_event=on_event)
        task.add_log(f"输出目录：{orch.out}")

        # 预检：目标站点不可达时立即失败，避免引擎超时后用户才知道原因
        blocked = []
        for plat in {classify(u, cfg)[1] for u in task.req.urls}:
            if orch.probe_ok(plat) is False:
                blocked.append(plat)
        if blocked:
            names = "、".join(blocked)
            raise RuntimeError(
                f"当前环境无法访问 {names}：出网被限制。"
                f"云端部署的沙箱仅能访问抖音；下载 TikTok 请在本机运行 "
                f"python dl.py \"链接\" 或 python web.py 后访问本机地址。"
            )

        results = orch.run(task.req.urls, task.req.limit, task.req.concurrency)
        ok = sum(1 for r in results if r.ok)
        task.add_log(f"任务结束：成功 {ok} / 失败 {len(results) - ok}")
        task.status = "done"
    except Exception as exc:
        task.error = f"{type(exc).__name__}: {exc}"
        task.add_log(f"任务异常：{task.error}", "error")
        task.status = "error"
    finally:
        task.finished = time.time()


# ---------------- routes ----------------
@app.get("/", response_class=HTMLResponse)
def index():
    html = STATIC / "index.html"
    if not html.exists():
        raise HTTPException(500, "缺少 static/index.html")
    return HTMLResponse(html.read_text(encoding="utf-8"))


@app.get("/api/diag")
def api_diag():
    """环境诊断：引擎可用性、ffmpeg、目标站点出网连通性。"""
    import shutil

    import httpx

    cfg = load_config()
    engines = build_engines(cfg)
    probe = {}
    for name, url in (("tiktok", "https://www.tiktok.com/"),
                      ("douyin", "https://www.douyin.com/")):
        t0 = time.time()
        try:
            r = httpx.get(url, headers={"User-Agent": cfg.user_agent},
                          proxy=cfg.proxy or None, timeout=12, follow_redirects=True)
            probe[name] = {"ok": True, "status": r.status_code,
                           "ms": int((time.time() - t0) * 1000)}
        except Exception as exc:
            probe[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160],
                           "ms": int((time.time() - t0) * 1000)}
    return {
        "engines": sorted(engines.keys()),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "proxy": cfg.proxy,
        "probe": probe,
    }


@app.get("/api/config")
def api_config():
    cfg = load_config()
    return {
        "output_dir": str(Path(cfg.output_dir).absolute()),
        "concurrency": cfg.concurrency,
        "timeout": cfg.timeout,
        "proxy": cfg.proxy,
        "engines": ["ytdlp", "f2"],
        "has_douyin_cookie": bool(cfg.cookie_for("douyin")),
        "has_tiktok_cookie": bool(cfg.cookie_for("tiktok")),
    }


@app.post("/api/tasks")
def api_create_task(req: JobRequest):
    urls = [u.strip() for u in req.urls if u and u.strip()]
    if not urls:
        raise HTTPException(400, "请至少提供一个链接")
    task = Task(req)
    with _lock:
        _tasks[task.id] = task
    threading.Thread(target=run_task, args=(task,), daemon=True).start()
    return {"id": task.id}


@app.get("/api/tasks")
def api_list_tasks():
    with _lock:
        return [t.to_dict() for t in sorted(_tasks.values(), key=lambda x: -x.created)]


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str):
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task.to_dict()


@app.delete("/api/tasks/{task_id}")
def api_delete_task(task_id: str):
    with _lock:
        return {"deleted": _tasks.pop(task_id, None) is not None}


@app.post("/api/parse")
def api_parse(req: JobRequest):
    """只解析不下载，返回作品清单。"""
    urls = [u.strip() for u in req.urls if u and u.strip()]
    if not urls:
        raise HTTPException(400, "请至少提供一个链接")
    try:
        cfg = make_cfg(req)
        engines = build_engines(cfg)
        orch = Orchestrator(cfg, engines, verbose=False)
        out = []
        for u in urls:
            try:
                for it in orch.collect(u, req.limit):
                    out.append({
                        "platform": it.platform, "id": it.item_id,
                        "type": it.media_type, "quality": it.quality_label,
                        "author": it.author.display, "desc": it.desc[:80],
                        "streams": len(it.video_urls) + len(it.image_urls),
                        "engine": it.engine,
                    })
            except Exception as exc:
                out.append({"platform": "-", "id": "-", "type": "-", "quality": "-",
                            "author": "-", "desc": f"解析失败：{exc}", "streams": 0,
                            "engine": "-"})
        return {"items": out}
    except Exception as exc:
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")


@app.get("/api/files")
def api_files():
    cfg = load_config()
    root = Path(cfg.output_dir)
    if not root.is_absolute():
        root = Path.cwd() / root
    if not root.exists():
        return {"root": str(root), "files": []}
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in {".mp4", ".jpg", ".jpeg", ".png", ".webp",
                                                ".mov", ".m4a", ".mp3"}:
            rel = p.relative_to(root).as_posix()
            files.append({"path": rel, "name": p.name,
                          "size": p.stat().st_size,
                          "mtime": int(p.stat().st_mtime)})
    return {"root": str(root), "files": files}


@app.get("/files/{rel:path}")
def api_download(rel: str):
    cfg = load_config()
    root = Path(cfg.output_dir)
    if not root.is_absolute():
        root = Path.cwd() / root
    target = (root / rel).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise HTTPException(403, "路径越界")
    if not target.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target, filename=target.name)


@app.post("/api/open-folder")
def api_open_folder():
    cfg = load_config()
    root = Path(cfg.output_dir)
    if not root.is_absolute():
        root = Path.cwd() / root
    root.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(root))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(root)], check=False)
        else:
            subprocess.run(["xdg-open", str(root)], check=False)
        return {"ok": True, "path": str(root)}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/cookie/export")
def api_export_cookie(payload: dict):
    platform = (payload or {}).get("platform", "douyin")
    if platform not in ("douyin", "tiktok"):
        raise HTTPException(400, "platform 只能是 douyin 或 tiktok")
    cookie = _from_browser(platform)
    if not cookie:
        raise HTTPException(404, f"浏览器中未找到 {platform} 的 Cookie，请先登录该站点")
    export_cookie_to_config(platform, cookie, None)
    return {"ok": True, "platform": platform, "len": len(cookie)}


STATIC.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="短视频批量下载器 Web 服务")
    # 云部署环境注入 PORT，默认绑定 0.0.0.0
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8770)))
    args = ap.parse_args()

    cfg = load_config()
    out = Path(cfg.output_dir)
    if not out.is_absolute():
        out = Path.cwd() / out
    out.mkdir(parents=True, exist_ok=True)

    print(f"Web 界面: http://{args.host}:{args.port}")
    print(f"输出目录: {out}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
