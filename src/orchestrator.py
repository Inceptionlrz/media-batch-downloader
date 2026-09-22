"""下载编排：解析 → 去重 → 并发落盘 → 汇总。"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .engines.base import Engine
from .models import MediaItem
from .router import classify
from .utils import ensure_dir, safe_filename


@dataclass
class Result:
    item: MediaItem
    files: list[Path] = field(default_factory=list)
    ok: bool = False
    error: str = ""
    engine: str = ""


class Index:
    """已下载记录，避免重复下载。"""

    def __init__(self, root: Path):
        self.path = ensure_dir(root) / ".dl_index.json"
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def key(self, item: MediaItem) -> str:
        return f"{item.platform}:{item.item_id}"

    def has(self, item: MediaItem) -> bool:
        return self.key(item) in self.data

    def add(self, item: MediaItem, files: list[Path]) -> None:
        self.data[self.key(item)] = {
            "url": item.url,
            "desc": item.desc[:80],
            "files": [str(f) for f in files],
            "engine": item.engine,
            "ts": int(time.time()),
        }

    def flush(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


class Orchestrator:
    def __init__(self, cfg: Config, engines: dict[str, Engine], verbose: bool = True,
                 on_event=None):
        self.cfg = cfg
        self.engines = engines
        self.verbose = verbose
        self.on_event = on_event
        self.total = 0
        self.done = 0
        self.out = Path(cfg.output_dir)
        if not self.out.is_absolute():
            self.out = Path.cwd() / self.out
        self.index = Index(self.out)
        self.skipped = 0

    def _emit(self, event: dict) -> None:
        """向外通知进度（Web / UI 层消费）；失败不影响主流程。"""
        if not self.on_event:
            return
        try:
            self.on_event(event)
        except Exception:
            pass

    def _log(self, msg: str, level: str = "info") -> None:
        if self.verbose:
            print(msg)
        self._emit({"type": "log", "level": level, "msg": msg})

    # ---------- engine selection ----------
    def pick_engines(self, platform: str) -> list[Engine]:
        order = self.cfg.engine_order.get(platform) or self.cfg.engine_order.get("*", ["ytdlp"])
        picked = []
        for name in order:
            eng = self.engines.get(name)
            if not eng:
                continue
            if platform in eng.platforms or "*" in eng.platforms:
                picked.append(eng)
        # 兜底：把其余可用引擎排在后面试试
        for eng in self.engines.values():
            if eng not in picked and ("*" in eng.platforms or platform in eng.platforms):
                picked.append(eng)
        return picked

    # ---------- network probe ----------
    _PROBE_TTL = 300  # 连通性缓存 5 分钟，避免每次失败都重复探测

    def probe_ok(self, platform: str) -> bool | None:
        """探测平台可达性；无法判断返回 None。"""
        import httpx

        targets = {"tiktok": "https://www.tiktok.com/", "douyin": "https://www.douyin.com/"}
        target = targets.get(platform)
        if not target:
            return None
        cache = getattr(self, "_probe_cache", None)
        if cache is None:
            cache = self._probe_cache = {}
        hit = cache.get(platform)
        now = time.time()
        if hit and now - hit[0] < self._PROBE_TTL:
            return hit[1]
        ok = False
        try:
            httpx.get(target, headers={"User-Agent": self.cfg.user_agent},
                      proxy=self.cfg.proxy or None, timeout=8, follow_redirects=True)
            ok = True
        except Exception:
            ok = False
        cache[platform] = (now, ok)
        return ok

    def _net_hint(self, platform: str) -> str:
        if self.probe_ok(platform) is False:
            if platform == "tiktok":
                return ("（当前部署环境无法访问 tiktok.com —— 云端沙箱仅支持抖音；"
                        "下载 TikTok 请在本机运行 python dl.py \"链接\" 或 python web.py）")
            return f"（当前环境无法访问 {platform} 站点，请检查网络或在 config.toml 配置代理）"
        return ""

    # ---------- collect ----------
    def collect(self, url: str, limit: int = 0) -> list[MediaItem]:
        final, platform, kind = classify(url, self.cfg)
        self._log(f"[解析] {final} → 平台={platform} 类型={kind}")

        if kind == "profile":
            items = self._collect_profile(final, platform, limit)
        else:
            items = [self._resolve_one(final, platform)]
        for it in items:
            it.raw.setdefault("requested_url", final)
        return items

    def _resolve_one(self, url: str, platform: str) -> MediaItem:
        errors = []
        for eng in self.pick_engines(platform):
            try:
                item = eng.resolve(url)
                if item.video_urls or item.image_urls:
                    return item
                errors.append(f"{eng.name}: 未取到媒体直链")
            except Exception as exc:
                errors.append(f"{eng.name}: {type(exc).__name__} {exc}")
        raise RuntimeError("所有引擎均失败 → " + " | ".join(errors) + self._net_hint(platform))

    def _collect_profile(self, url: str, platform: str, limit: int) -> list[MediaItem]:
        errors = []
        for eng in self.pick_engines(platform):
            try:
                items = eng.profile(url, limit)
                if items:
                    return items
                errors.append(f"{eng.name}: 主页未解析到作品")
            except Exception as exc:
                errors.append(f"{eng.name}: {type(exc).__name__} {exc}")
        raise RuntimeError("主页解析失败 → " + " | ".join(errors) + self._net_hint(platform))

    # ---------- paths ----------
    def dest_dir(self, item: MediaItem) -> Path:
        author = safe_filename(item.author.display, 40) or "unknown"
        return ensure_dir(self.out / safe_filename(item.platform or "generic", 20) / author)

    def stem(self, item: MediaItem) -> str:
        date = time.strftime("%Y%m%d", time.localtime(item.create_time)) if item.create_time else ""
        desc = safe_filename(item.desc, 40)
        parts = [p for p in (date, item.item_id, desc) if p]
        return "_".join(parts) or item.item_id or "media"

    # ---------- download ----------
    def save(self, item: MediaItem) -> Result:
        res = Result(item=item, engine=item.engine)
        dest = self.dest_dir(item)
        stem = self.stem(item)
        for eng_name in ([item.engine] if item.engine in self.engines else
                         [e.name for e in self.pick_engines(item.platform)]):
            eng = self.engines.get(eng_name)
            if not eng:
                continue
            for attempt in range(1, self.cfg.retries + 1):
                try:
                    files = eng.download(item, dest, stem)
                    if files:
                        res.files, res.ok, res.engine = files, True, eng.name
                        return res
                    res.error = f"{eng.name}: 未产出文件"
                except Exception as exc:
                    res.error = f"{eng.name} 第{attempt}次: {type(exc).__name__} {exc}"
                    if attempt < self.cfg.retries:
                        time.sleep(1.5 * attempt)
        return res

    def run(self, urls: list[str], limit: int = 0, concurrency: int | None = None) -> list[Result]:
        items: list[MediaItem] = []
        for u in urls:
            try:
                items.extend(self.collect(u, limit))
            except Exception as exc:
                self._log(f"[失败] 解析 {u} → {exc}", "error")

        todo, self.skipped = [], 0
        for it in items:
            if self.index.has(it):
                self.skipped += 1
                self._log(f"[跳过] 已下载 {it.platform}:{it.item_id}")
            else:
                todo.append(it)

        self.total = len(todo)
        self.done = 0
        self._log(f"共解析 {len(items)} 个作品，待下载 {len(todo)}，跳过已存在 {self.skipped}")
        self._emit({"type": "phase", "phase": "downloading",
                    "total": self.total, "skipped": self.skipped, "parsed": len(items)})
        results: list[Result] = []
        if not todo:
            self._emit({"type": "done", "ok": 0, "fail": 0, "skipped": self.skipped})
            return results

        workers = max(1, concurrency or self.cfg.concurrency)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.save, it): it for it in todo}
            for fut in as_completed(futures):
                res = fut.result()
                self.done += 1
                name = res.files[0].name if res.files else "-"
                self._log(f"[{'OK' if res.ok else 'FAIL'}] ({self.done}/{self.total}) "
                          f"{res.item.platform}:{res.item.item_id} {res.item.quality_label} → {name}"
                          + ("" if res.ok else f"  ({res.error})"),
                          "info" if res.ok else "error")
                self._emit({
                    "type": "item",
                    "status": "ok" if res.ok else "fail",
                    "platform": res.item.platform,
                    "id": res.item.item_id,
                    "quality": res.item.quality_label,
                    "engine": res.engine,
                    "desc": res.item.desc[:80],
                    "author": res.item.author.display,
                    "file": name,
                    "error": "" if res.ok else res.error,
                    "done": self.done,
                    "total": self.total,
                })
                if res.ok:
                    self.index.add(res.item, res.files)
                results.append(res)
        self.index.flush()
        ok = sum(1 for r in results if r.ok)
        self._emit({"type": "done", "ok": ok, "fail": len(results) - ok, "skipped": self.skipped})
        return results
