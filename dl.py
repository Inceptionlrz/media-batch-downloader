"""命令行入口：单资源下载 / 博主主页批量下载。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import load_config  # noqa: E402
from src.cookies import _from_browser, ensure_cookies, export_cookie_to_config  # noqa: E402
from src.engines.f2_engine import F2Engine  # noqa: E402
from src.engines.ytdlp_engine import YtdlpEngine  # noqa: E402
from src.orchestrator import Orchestrator  # noqa: E402


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


def read_url_file(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dl",
        description="TikTok / 抖音 等平台链接批量下载器（原画质、无水印）",
    )
    ap.add_argument("urls", nargs="*", help="作品链接或博主主页链接")
    ap.add_argument("-f", "--file", help="包含链接的文本文件，每行一个")
    ap.add_argument("-o", "--output", help="输出目录，覆盖 config.toml")
    ap.add_argument("-n", "--limit", type=int, default=0, help="主页最多下载条数，0=全部")
    ap.add_argument("-j", "--concurrency", type=int, default=None, help="并发数")
    ap.add_argument("--engine", help="强制指定引擎：ytdlp 或 f2")
    ap.add_argument("--proxy", help="代理，如 http://127.0.0.1:7897")
    ap.add_argument("--browser-cookie", action="store_true",
                    help="允许从本机 Chrome/Edge/Firefox 读取站点 Cookie（默认关闭）")
    ap.add_argument("--export-cookie", choices=["douyin", "tiktok"],
                    help="从本机浏览器导出站点 Cookie 并写入 config.toml")
    ap.add_argument("--dry-run", action="store_true", help="仅解析不下载")
    ap.add_argument("--config", help="配置文件路径")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config) if args.config else None)

    if args.export_cookie:
        cookie = _from_browser(args.export_cookie)
        if not cookie:
            print(f"未在浏览器中找到 {args.export_cookie} 的 Cookie。"
                  f"请先在 Chrome/Edge 打开并登录 {args.export_cookie}.com 后重试。")
            return 1
        export_cookie_to_config(args.export_cookie, cookie, None)
        print(f"已写入 config.toml → [{args.export_cookie}] cookie")
        return 0

    if args.output:
        cfg.output_dir = args.output
    if args.proxy:
        cfg.proxy = args.proxy
    if args.concurrency:
        cfg.concurrency = args.concurrency
    if args.engine:
        for plat in list(cfg.engine_order):
            cfg.engine_order[plat] = [args.engine]

    urls = list(args.urls)
    if args.file:
        urls.extend(read_url_file(args.file))
    if not urls:
        ap.print_help()
        return 1

    if args.browser_cookie:
        ensure_cookies(cfg, allow_browser=True)
    else:
        ensure_cookies(cfg, allow_browser=False)

    engines = build_engines(cfg)
    if not engines:
        print("无可用引擎，请先 pip install -r requirements.txt")
        return 2

    orch = Orchestrator(cfg, engines)
    if args.dry_run:
        for u in urls:
            for it in orch.collect(u, args.limit):
                print(f"{it.platform}:{it.item_id} [{it.media_type}] {it.quality_label} "
                      f"| {it.author.display} | {it.desc[:40]} | streams={len(it.video_urls)+len(it.image_urls)}")
        return 0

    results = orch.run(urls, args.limit)
    ok = sum(1 for r in results if r.ok)
    if not results and orch.skipped:
        print("全部作品此前已下载完毕，无需重复执行")
        return 0
    print(f"\n完成：成功 {ok} / 失败 {len(results) - ok}；输出目录：{orch.out}")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
