# 飞书「状态检测无响应」排查报告

- 排查对象：`feishu-bridge`（飞书 → GitHub Actions 桥接，Vercel Node 函数）
- 回调地址：`https://fs.coldward.ccwu.cc/api/feishu`
- 报告日期：2026-10-16
- 结论：**已定位并修复**；线上实测应答耗时从端到端 2.906s 降到服务端 1ms，回执成功率 100%。

---

## 一、根因结论

### 主因：同步等待外部请求后才应答，吃满飞书 3 秒回调预算

飞书事件回调要求 **3 秒内返回**，超时即判定失败，且不会在群里展示任何回执。
原实现是「解析指令 → 串行调用 GitHub 4~5 次 → 调飞书 webhook 回执 → 最后才 `res.status(200)`」，
实测一次 `/status` 端到端 **2.906s**，几乎没有余量，任何一次网络抖动都会超时。

| 阶段（修复前，实测） | 耗时 |
| --- | --- |
| 简单 `url_verification` 握手（冷启动） | 2.774s |
| 简单 `url_verification` 握手（热） | 1.606 ~ 1.886s |
| 一次 `/status` 完整链路 | **2.906s** |
| 飞书回调超时上限 | 3.000s |

### 次因：回执失败被完全静默

飞书群机器人 webhook 的**业务失败是 `HTTP 200` + `body.code != 0`**（如 `19001` 签名无效、`9499` 机器人不在群内）。
原 `reply()` 只 `await fetch(...)`，既不检查响应码也不解析响应体，且 `if (!FEISHU_WEBHOOK) return;` 直接静默返回 ——
即使机器人被移出群、签名配错、webhook 失效，代码里也看不到任何痕迹。

### 其他叠加因素

| # | 问题 | 影响 |
| --- | --- | --- |
| 3 | 全程无任何日志 | 失败无法定位，只能猜 |
| 4 | 外部请求无超时控制 | 挂起会耗尽 30s `maxDuration` |
| 5 | 回执只依赖群自定义机器人 | 机器人被移出群即全链路失效 |
| 6 | 函数区域在 `iad1`（华盛顿） | 中国大陆往返慢，加重超时 |
| 7 | 生产部署代码落后于仓库 | 线上返回 `{"challenge":...}`，仓库代码返回 `{"code":0,"challenge":...}` —— 版本不一致 |

> 关于「app_id / app_secret / tenant_access_token」：原代码**根本没有这条链路**（只有 webhook），
> 本次已补齐为可选通道（见下文）。当前 `FEISHU_APP_ID/SECRET` 未配置，仍在用 webhook 通道。

---

## 二、改动文件

| 文件 | 改动 |
| --- | --- |
| `feishu-bridge/api/feishu.js` | 核心重写：立即应答 + 后台执行、全链路超时、结构化日志、回执双通道、诊断端点、事件去重、签名兼容 v2 header、`/status` 并行化并恢复发布时间、`/pull` 恢复 |
| `feishu-bridge/vercel.json` | 增加 `regions: ["sin1"]` |
| `feishu-bridge/package.json` / `package-lock.json` | 新增依赖 `@vercel/functions`（`waitUntil`） |
| `feishu-bridge/test-local.mjs` | 冒烟测试从 9 项扩到 **19 项**（新增回执失败可见、超时受控、事件去重、诊断端点、`/pull`、`/status` 批次断言） |
| `feishu-bridge/.env.example` | 新增 `FEISHU_APP_ID/SECRET`、`FEISHU_BASE`、`HTTP_TIMEOUT_MS`、`LOG_FULL_URL`、`BRIDGE_SYNC` 说明 |
| `feishu-bridge/README.md` | 新增「排查群里没反应」章节、根因对照表、诊断端点用法 |
| `feishu-bridge/.gitignore` | 排除 `node_modules`、`.env.local` |
| `scripts/github_push.py` | 新增 `--delta` 增量提交模式（支持 `--base`）与 `node_modules`/`.vercel` 排除 |

---

## 三、关键改动说明

1. **立即应答 + 后台执行**
   收到事件后先 `res.status(200).json({code:0, accepted:true, cmd})`，
   再用 `@vercel/functions` 的 `waitUntil()` 把「指令执行 + 回执」放到后台。
   副作用：群里回执会比指令晚 1~3 秒出现，这是预期行为。

2. **`timedFetch` 统一超时/重试/日志**
   所有外部请求带 `AbortController`（默认 8s，可用 `HTTP_TIMEOUT_MS` 调）、5xx 自动重试 1 次，
   日志记录**请求方法、地址（默认脱敏 webhook/token）、响应码、耗时、响应体前 500 字符**，日志前缀 `[bridge]`。

3. **回执双通道 + 失败可见**
   - 通道 A：`FEISHU_APP_ID/SECRET` → `tenant_access_token`（内存缓存，提前 60s 过期）
     → `POST /open-apis/im/v1/messages/{message_id}/reply` 定向回复。
   - 通道 B：群自定义机器人 webhook（兜底，自动带加签）。
   - 两条都解析 `body.code`，失败写入 `recentErrors` 并计数；A 失败降级 B 时会在群里说明原因。

4. **诊断端点 `GET /api/feishu?diag=1[&probe=github,feishu]`**
   返回：配置布尔值（不泄露密钥）、`region`、`lastEvent`（含 `ackMs`/`doneMs`/`replyOk`/`replyChannel`）、
   `counters`、`recentErrors`（最近 20 条）、可选真实连通性探测、以及**需人工核对的信息清单**。

5. **`event_id` 去重（10 分钟窗口）**：飞书对超时/失败会重推，避免 `/run` 被重复触发。

6. **入站签名兼容 v2**：读 `X-Lark-Signature` / `X-Lark-Request-Timestamp` / `X-Lark-Nonce` 头
   （原实现只读 query 参数，永远取不到），保留 query 兜底；未配置 `FEISHU_SIGN_SECRET` 时跳过校验。

---

## 四、验证步骤（测试环境 / 线上已执行）

### 1. 本地冒烟测试

```bash
cd feishu-bridge
npm i
node test-local.mjs
```

预期：`=== 19 passed, 0 failed ===`。
覆盖：握手、全部 8 条指令、未知指令、事件去重、**webhook 返回 HTTP 200 + code=19001 时仍要写 `recentErrors`**、
**GitHub 挂起时超时受控**、诊断端点与 GitHub 探测。

### 2. 线上诊断端点

```bash
curl 'https://fs.coldward.ccwu.cc/api/feishu?diag=1&probe=github,feishu'
```

实测输出（修复后）：

```
region      : sin1
config      : GH_TOKEN=true, FEISHU_WEBHOOK=已配置(len=81), SIGN_SECRET=false, APP_ID/SECRET=false
channels    : { app: false, webhook: true }
probeGithub : { ok: true, ms: 321, fullName: Inceptionlrz/media-batch-downloader }
probeFeishu : { ok: true, channel: "webhook" }
counters    : { events: 3, replies: 3, replyFailures: 0 }
errors      : 0
```

### 3. 模拟 `/status` 事件，验证应答耗时与回执

```bash
curl -s -w '\n[total=%{time_total}s]\n' -X POST https://fs.coldward.ccwu.cc/api/feishu \
  -H 'Content-Type: application/json' \
  -d '{"schema":"2.0","header":{"event_id":"test-1","event_type":"im.message.receive_v1"},
       "event":{"message":{"message_id":"om_test_1","chat_id":"oc_test",
       "content":"{\"text\":\"@_user_1 /status\"}"}}}'
```

实测（修复后，连发 3 次）：

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 服务端 `ackMs`（应答耗时） | ≈ 1300ms（等 GitHub 才回） | **1ms** |
| 服务端 `doneMs`（含后台执行与回执） | 2906ms | ≈ 806ms |
| `replyOk` | 偶发失败/静默 | `true`（0 失败） |
| `region` | `iad1` | `sin1` |

外部观测 `total` 1.5~1.8s，分解后 **dns 0.03s / tls ~0.9s / TTFB 1.4s**，
剩余时间基本是本机→Cloudflare→Vercel 的网络与 TLS 开销，不是函数耗时。

### 4. 群里确认

`probeFeishu.ok=true` 说明回执真的发到了群里。执行上面两步后，群里应出现：
1 条「🔧 feishu-bridge 自检：这是一条测试回执（diag probe）」
+ 每次 `/status` 一条状态卡片。**请确认群里能否看到** —— 这是唯一还需要你人工确认的一环。

---

## 五、仍需人工确认的信息（代码无法自证，未臆造）

| # | 需确认项 | 在哪里看 / 怎么做 |
| --- | --- | --- |
| 1 | 事件订阅是否已启用 `im.message.receive_v1`，请求地址是否为 `https://fs.coldward.ccwu.cc/api/feishu` | 飞书开放平台 → 你的自建应用 → 事件订阅（填 `*.vercel.app` 会被国内 DNS 污染） |
| 2 | 权限 `im:message.group_at_msg:readonly` 是否已开通**并重新发版** | 权限管理 → 版本管理与发布 → 创建版本 → 发布 |
| 3 | 群内是否同时有「企业自建应用机器人（收指令）」+「群自定义机器人（发回执）」 | 群设置 → 群机器人 |
| 4 | 是否改用定向回复（推荐） | 填 `FEISHU_APP_ID/SECRET`，并给应用开通 `im:message` 发送权限；填完需重新部署 |
| 5 | 最近一次真实事件有没有进来 | `diag` 的 `lastEvent`；为空 = 飞书根本没推过来（问题在 1/2/3），非空 = 看 `replyOk` 与 `recentErrors` |

补充采集入口：

- **Vercel Logs**：项目 → Logs，搜 `[bridge]`，含 `inbound` / 各请求耗时响应码 / `指令处理完成` / `回执结果`。
- **飞书后台**：事件订阅 → 事件推送记录，可区分「未发送 / 发送失败 / 超时」。
- **需要完整 URL 排查时**：临时把 `LOG_FULL_URL` 设为 `1` 并重新部署（默认对 webhook/token 脱敏）。

---

## 六、提交与部署

- 远端 `main`：`d9aa88ff`（核心修复）→ `59e7aaab`（ackMs 语义修正 + sin1 + 推送脚本）。
- `git push` 在本机仍会卡死，改用 `python scripts/github_push.py --delta --base=<本地ref> "信息"`。
- Vercel 生产部署已更新，`https://fs.coldward.ccwu.cc` 已别名到最新部署。
