# 短视频批量下载器（TikTok / 抖音）

通过链接批量下载 TikTok、抖音等平台内容的命令行 + Web 工具，**输出平台原画质、无水印**的媒体文件。

- 代码仓库：https://github.com/Inceptionlrz/media-batch-downloader
- 在线部署：https://media-batch-dl.app.workbuddy.host/

## 技术选型（二次开发基座）

在 GitHub 检索同类项目后，选定两个成熟开源项目作为基座，而非从零实现：

| 基座 | 版本 | 角色 | 选型理由 |
|---|---|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 2026.08.19 | 通用引擎 | 1000+ 站点 extractor；格式选择器成熟，天然支持「最高画质视频 + 最高音质音频」无损合并；TikTok 单作品与博主主页均已实测跑通 |
| [f2](https://github.com/Johnserf-Seed/f2) | 0.0.1.7 | 抖音/TikTok 专用引擎 | 直连平台 Web 接口，可取 `bit_rate` 多档清晰度中的最高档无水印地址；提供 `fetch_one_video` / `fetch_user_post_videos` 编程接口，适合二次开发 |

同类候选（未采用）：`Evil0ctal/Douyin_TikTok_Download_API`（20.2k stars，需 Postgres + 常驻服务，重）、`JoeanAmier/TikTokDownloader`（16.2k stars，交互式/配置驱动，二次开发成本高）。

本项目在两者之上补了一层：**统一数据模型 + 链接路由 + 引擎回退 + 并发调度 + 去重索引**。

## 安装

```bash
pip install -r requirements.txt
# 需要 ffmpeg（合并音视频/封装），且已在 PATH 中
ffmpeg -version
```

## Web 界面

```bash
pip install -r requirements.txt     # 含 fastapi / uvicorn
python web.py                       # 默认 http://127.0.0.1:8770
python web.py --host 0.0.0.0 --port 9000
```

浏览器打开后可直接：粘贴链接（每行一个）→ 解析预览 / 开始下载 → 实时看进度与日志 → 在线预览下载文件。

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/parse` | POST | 只解析，返回作品清单、画质、作者 |
| `/api/tasks` | POST / GET | 创建下载任务 / 列出任务 |
| `/api/tasks/{id}` | GET / DELETE | 任务详情（进度、日志、逐条结果）/ 删除 |
| `/api/files` | GET | 输出目录文件清单 |
| `/files/{path}` | GET | 下载单个文件 |
| `/api/open-folder` | POST | 打开输出目录 |
| `/api/cookie/export` | POST | 从浏览器导出 Cookie 写入 config.toml |

前端每 1.5 秒轮询一次运行中任务，无需手动刷新。任务记录保存在内存，重启服务后清空（已下载索引仍持久化在 `output/.dl_index.json`）。

> 依赖冲突提示：`f2` 要求 `websockets<13`，安装 `uvicorn[standard]` 会把它升到 17 导致 f2 导入失败。requirements.txt 已固定 `websockets==12.0`，若已误升级请执行 `pip install "websockets==12.0"`。

### 云端沙箱的网络边界（实测）

部署环境位于国内，`/api/diag` 实测结果：

| 目标 | 云端沙箱 | 本机（走代理） |
|---|---|---|
| TikTok | ❌ 不可达（TLS 连接被关闭） | ✅ 可达 |
| 抖音 | ✅ 可达（约 90ms） | ✅ 可达 |

因此：**云端部署的界面适合下抖音（需配置 Cookie），下 TikTok 请用本地运行**（`python web.py` 或 CLI）。

工具已内置两道防线，避免提交后干等：

1. 打开页面即诊断，TikTok / 抖音不可达时顶部显示黄色警告条；
2. 任务启动前预检目标站点，不可达时 **8 秒内**直接失败并提示原因（此前需等引擎超时约 80 秒）。

## GitHub Actions 批量下载（TikTok 的首选方案）

GitHub-hosted runner 位于境外数据中心，**出网不受国内沙箱限制，TikTok 可直接下载**。
Action 是批处理形态（无常驻 Web 界面），触发入口就是 GitHub 的 Actions 页面。

### 工作流一：`批量下载`（手动触发）

仓库 → **Actions** → 选「批量下载」→ **Run workflow**，填写：

| 输入 | 说明 | 默认 |
|---|---|---|
| `urls` | 链接列表，每行一个（作品链接 / 主页链接 / 短链均可） | 必填 |
| `limit` | 每个主页最多下载条数，`0` = 全部 | `20` |
| `concurrency` | 并发数 | `4` |
| `engine` | 强制引擎，留空按配置自动回退 | 空 |
| `delivery` | `both` / `artifact` / `release` | `both` |
| `retention` | Artifact 保留天数 | `7` |

运行结束后：

- **Artifact**：Actions 页面直接下载（需登录 GitHub）
- **Release**：`batch-<运行号>` tag，附带 `media-batch.zip` 与文件清单，**匿名可下载的公开直链**
  `https://github.com/<owner>/<repo>/releases/download/batch-<N>/media-batch.zip`
- **Job Summary**：Actions 运行页显示本次文件数与逐条清单

### 工作流二：`博主更新监控`（定时增量）

每 6 小时跑一次，只下载上次之后的新作品。去重索引 `.dl_index.json` 通过 Actions Cache
持久化，无需入库。有新增才产 Artifact（保留 30 天），无新增则空跑。

监控目标来源（二选一，都会读）：

1. 手动 Run workflow 时填 `urls`；
2. 仓库 **Settings → Secrets and variables → Actions → Variables** 新建 `WATCH_URLS`，每行一个主页链接。

> GitHub `schedule` 是尽力而为，通常延迟 5~30 分钟，不适合准实时场景。

### 配置 Cookie（抖音必需）

Actions 环境没有浏览器，只能走 Secrets：

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | 用途 |
|---|---|
| `DOUYIN_COOKIE` | 抖音登录 Cookie（抖音接口风控严格，无 Cookie 常返回验证页） |
| `TIKTOK_COOKIE` | TikTok Cookie（通常可留空，实测未登录即可下载） |

代码已支持 `DOUYIN_COOKIE` / `TIKTOK_COOKIE` 环境变量，无需改文件。取 Cookie：
浏览器登录抖音 → F12 → Network → 任选请求 → 复制 `Cookie` 请求头整串。

### 配额与限制

| 项 | 免费额度 |
|---|---|
| 私有仓库 | 2000 分钟/月，Artifact 存储 500MB |
| 公开仓库 | 无限分钟数，Artifact 存储无限 |
| 单次 Job | 最长 6 小时（本工作流设 330 分钟） |

大批量下载建议：仓库设为公开，或改用 Release 直链 + 缩短 `retention`。

## 使用（命令行）

```bash
# 1）单资源：下载一个帖子里的视频或图集
python dl.py "https://www.tiktok.com/@tiktok/video/7106594312292453675"
python dl.py "https://v.douyin.com/xxxxxxx/"

# 2）博主批量：解析主页，逐个下载
python dl.py "https://www.tiktok.com/@tiktok"
python dl.py "https://www.douyin.com/user/MS4wLjABAAAA..." -n 50

# 3）批量链接文件（每行一个链接，# 开头为注释）
python dl.py -f urls.txt -j 8 -o D:\downloads

# 仅解析不下载（检查能拿到什么画质）
python dl.py "https://www.tiktok.com/@tiktok" -n 5 --dry-run
```

常用参数：

| 参数 | 说明 |
|---|---|
| `-o, --output` | 输出目录，覆盖 config.toml |
| `-n, --limit` | 主页最多下载条数，0 = 全部 |
| `-j, --concurrency` | 并发数 |
| `--proxy` | 代理，如 `http://127.0.0.1:7897`（TikTok 通常必需） |
| `--engine` | 强制指定引擎 `ytdlp` 或 `f2` |
| `--browser-cookie` | 允许从本机 Chrome/Edge/Firefox 读取站点 Cookie（默认关闭） |
| `--export-cookie` | 从浏览器导出 Cookie 写入 `config.toml`（`douyin` / `tiktok`） |
| `--dry-run` | 只解析打印，不落盘 |

输出结构：

```
output/
└── tiktok/
    └── <博主昵称>/
        ├── 20260815_7106594312292453675_标题.mp4
        └── ...
```

## 画质如何保证

1. **格式选择**：yt-dlp 使用 `bv*+ba/b`（最佳视频流 + 最佳音频流，回退最佳单流），合并时只做封装不做重编码（`ffmpeg -c copy`），不产生二次压缩。
2. **多档择优**：f2 引擎遍历 `video.bit_rate[]`，按 `height*10000+width` 打分取最高档的 `play_addr`（平台无水印流），而非默认的 `download_addr`（可能带水印）。
3. **图集取原图**：`images[].url_list` 取末位（最高清原图），不取缩略图。
4. **可核验**：`--dry-run` 会打印每条的分辨率；落盘后可用 `ffprobe` 复核。

## Cookie 说明

- **TikTok**：实测无需 Cookie，单作品与博主主页均可直接下载。
- **抖音**：平台 Web 接口要求「新鲜 Cookie」（不必登录），且实测未登录的 fresh Cookie 仍会被 403 拦截 —— **需要在浏览器登录 douyin.com 后导入 Cookie**。获取顺序：

  1. `config.toml` 的 `[douyin] cookie` 或环境变量 `DOUYIN_COOKIE`
  2. `--browser-cookie`：运行时从本机浏览器读取
  3. 自动申请未登录 fresh Cookie（`ttwid` + `__ac_nonce`，仅作为兜底，成功率有限）

  推荐一步到位：

  ```bash
  # 在 Chrome/Edge 打开并登录 https://www.douyin.com 后执行
  python dl.py --export-cookie douyin
  ```

  注意：yt-dlp 不支持抖音主页 URL，抖音批量下载依赖 f2 引擎，因此 Cookie 是必需项。

## 目录结构

```
dl.py                     CLI 入口
web.py                    Web 界面（FastAPI）
.github/workflows/
  batch-download.yml      手动触发的批量下载流水线
  watch.yml               定时监控博主更新（增量）
src/
  config.py               配置加载（config.toml）
  cookies.py              Cookie 多策略获取
  models.py               统一数据模型 MediaItem / Author
  router.py               链接识别：平台 + 单作品/主页 + 短链还原
  orchestrator.py         解析 → 去重 → 并发落盘 → 索引
  utils.py                命名、流式下载、ffmpeg 无损合并
  engines/
    base.py               引擎抽象
    ytdlp_engine.py       通用引擎
    f2_engine.py          抖音/TikTok 专用引擎（含旧版回退）
config.example.toml       配置模板
```

## 已知限制

- 抖音 Web 接口风控升级频繁，`fetch_one_video` 偶发失败；已实现 f2 → yt-dlp 自动回退，仍失败时建议配置真实 Cookie。
- 已下载作品记录在 `output/.dl_index.json`，重复执行自动跳过；删除该文件可强制重下。
- 仅支持公开可见内容；私密/登录可见内容不在能力范围内。
- GitHub Actions 为批处理形态，不能承载常驻 Web 界面；Web 界面请用本地 `python web.py` 或云端部署。

## 免责声明

仅供个人学习研究与对自己拥有权利的内容做离线备份。请遵守各平台服务条款与目标内容版权，勿用于商业分发或侵权用途。
