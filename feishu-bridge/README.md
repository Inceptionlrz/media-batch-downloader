# 飞书 → GitHub Actions 桥接（feishu-bridge）

让你在**飞书群**里直接发指令，控制 `media-batch-downloader` 的「博主更新监控」任务：
立即运行、调整频次、暂停/恢复、增删监控博主、查询状态、查看最新下载批次。

GitHub Actions 本身收不到飞书消息，也改不了写死在 YAML 里的 cron；
本函数作为公网端点翻译指令并调用 GitHub API，把控制权交回聊天框。

## 指令一览（群里 @机器人 发送）

| 指令 | 作用 |
| --- | --- |
| `/run` | 立即触发一次监控（不等下一个周期） |
| `/freq 2h` | 调整频次：`2h`/`6h`/`12h`/`1h`/`30m`…，改写 `watch.yml` 的 cron 并提交 |
| `/pause` | 暂停监控（置 `WATCH_PAUSED=true`，任务整轮跳过） |
| `/resume` | 恢复监控 |
| `/add @user` | 新增监控博主（支持 `@名` 或完整主页链接） |
| `/del @user` | 移除监控博主 |
| `/status` | 返回最近运行结果、监控对象、当前 cron、是否暂停、最近下载批次（含北京时间） |
| `/pull` | 只看最近下载批次：直链 + 资源大小 + 内容概要 |

执行结果会回执到群里。

## 架构

```
飞书群 @机器人 ──(HTTPS 回调)──> Vercel 函数 (/api/feishu)
                                    │ 立刻回 200（<3s，满足飞书回调超时）
                                    ├─ waitUntil 后台：解析指令
                                    ├─ 调 GitHub API（GH_TOKEN）
                                    │    · 触发 dispatch / 改 cron / 改 Variables / 读 Release
                                    └─ 回执飞书群（定向回复 API 优先，失败降级 webhook）
```

## 一、飞书侧配置（一次性）

1. **企业自建应用机器人（收指令）**
   - 开放平台 → 事件订阅 → 添加事件「**接收消息 im.message.receive_v1**」，
     请求地址填 `https://fs.coldward.ccwu.cc/api/feishu`
     （**不要**填 `*.vercel.app`，国内 DNS 会被污染）。
   - 权限管理 → 至少开通 `im:message.group_at_msg:readonly`（群 @ 机器人消息）。
   - 改完权限必须「版本管理与发布 → 创建版本 → 发布」才生效。
2. **群自定义机器人（发回执，兜底通道）**
   - 群设置 → 群机器人 → 添加自定义机器人，记下 webhook（`https://open.feishu.cn/open-apis/bot/v2/hook/xxx`）
     → 填到 `FEISHU_WEBHOOK`（也等于仓库里的 `NOTIFY_WEBHOOK`）。
3. **可选：定向回复（推荐）**
   - 填 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，函数会先尝试
     `POST /open-apis/im/v1/messages/{message_id}/reply` 定向回复，
     不再依赖群自定义机器人是否还在群里；失败自动降级到 webhook。
   - 需给应用开通消息发送权限（`im:message`）。
4. **安全设置**：建议留空（不加签），与仓库现有通知配置一致；
   若开启加签，把密钥填到 `FEISHU_SIGN_SECRET`（入站校验 v2 签名 + 出站自动带签名）。

> 首次保存回调地址时飞书会发一次 `url_verification`，函数自动回 `challenge`。

## 二、本地调试

```bash
cp .env.example .env.local      # 填好 GH_TOKEN / FEISHU_WEBHOOK 等
npm i                           # 仅 @vercel/functions（waitUntil）
npm i -g vercel                 # 或 npx vercel
vercel dev                      # 默认 http://localhost:3000
# 另开终端模拟飞书回调：
curl -X POST http://localhost:3000/api/feishu \
  -H 'Content-Type: application/json' \
  -d '{"schema":"2.0","header":{"event_type":"im.message.receive_v1"},"event":{"message":{"message_id":"om_1","content":"{\"text\":\"@_user_1 /status\"}"}}}'
```

冒烟测试（mock 掉 GitHub/Feishu，覆盖指令分发、去重、回执失败可见、超时、诊断）：

```bash
node test-local.mjs     # 19 项，全绿才算通过
```

## 三、Vercel 部署

```bash
cd feishu-bridge
vercel              # 按提示登录并部署；首部署会生成 .vercel 目录
vercel --prod       # 发布到生产域名
```

部署后在 **Vercel 项目 → Settings → Environment Variables** 填入：

| 变量 | 说明 |
| --- | --- |
| `GH_TOKEN` | GitHub PAT（需 `repo` 权限） |
| `GH_OWNER` | `Inceptionlrz` |
| `GH_REPO` | `media-batch-downloader` |
| `GH_WORKFLOW` | `watch.yml` |
| `GH_BRANCH` | `main` |
| `FEISHU_WEBHOOK` | 群自定义机器人 webhook（回执兜底） |
| `FEISHU_SIGN_SECRET` | 可选；开启加签时填，否则留空 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 可选；启用定向回复 |
| `FEISHU_BASE` | 可选；默认 `https://open.feishu.cn` |
| `HTTP_TIMEOUT_MS` | 可选；默认 `8000` |
| `LOG_FULL_URL` | 可选；置 `1` 打印完整 URL（默认脱敏） |

改完环境变量**重新部署一次**使其生效。

## 四、排查「群里发了指令却没任何反应」

按顺序查，前三步不需要进 Vercel：

1. **看诊断端点**（最直接，能立刻区分是哪一段坏了）：

   ```bash
   curl 'https://fs.coldward.ccwu.cc/api/feishu?diag=1&probe=github,feishu'
   ```

   返回里重点看：
   - `config`：各项凭证是否为 `true`（`false` 说明没配置）
   - `region`：函数所在区域，越远回调往返越慢（飞书只给 3 秒）
   - `probeGithub`：GitHub 是否连通、耗时多少（不通则指令必然失败）
   - `probeFeishu`：回执通道是否真的能发出去（`ok:false` 且带 `code`/`msg` 就是飞书侧拒绝，
     如 `9499` 机器人不在群内、`19001` 签名无效）
   - `lastEvent`：最近一次收到的事件（为空 = 飞书根本没推过来，问题在事件订阅/权限/群成员）
   - `recentErrors`：最近 20 条错误，含阶段、响应码与飞书错误信息

2. **看 Vercel Logs**：项目 → Logs，搜 `[bridge]`，每条外部请求都会打印
   `请求地址 / 方法 / 响应码 / 耗时 / 响应体`，以及 `inbound`、`指令处理完成`、`回执结果`。

3. **看飞书后台**：事件订阅 → 事件推送记录，区分「未发送 / 发送失败 / 超时」。

### 常见根因对照表

| 现象 | 根因 | 处理 |
| --- | --- | --- |
| `lastEvent` 为空 | 事件未订阅或权限未开或未发版 | 勾 `im.message.receive_v1`、开 `im:message.group_at_msg:readonly`、重新发版 |
| `lastEvent` 为空 + 回调填的 `*.vercel.app` | 国内 DNS 污染 | 改填 `https://fs.coldward.ccwu.cc/api/feishu` |
| `probeFeishu.ok=false`, `code=9499` | 群自定义机器人被移出群 | 加回机器人，或改用 `FEISHU_APP_ID/SECRET` 定向回复 |
| `probeFeishu.ok=false`, `code=19001` | 加签校验失败 | 关闭加签，或把正确密钥填进 `FEISHU_SIGN_SECRET` |
| `probeGithub.ok=false` | `GH_TOKEN` 失效/权限不足 | 换 PAT（需 `repo`） |
| 日志有事件但群里仍无消息 | 回执通道失败（见 `recentErrors`） | 按上面的 code 处理 |
| 飞书提示回调超时 | 之前是同步等 GitHub 才回 200，常超过 3 秒 | 已改为立刻回 200 + 后台执行；若仍超时看 `region` 与 `probeGithub.ms` |

## 注意事项

- **飞书回调只有 3 秒预算**：函数收到事件后立即回 200，指令执行与回执在后台继续，
  因此群里回执会比指令晚 1~3 秒才出现，这是预期行为。
- **事件去重**：飞书对超时/失败会重推，函数按 `event_id` 去重（10 分钟窗口），避免 `/run` 被重复触发。
- **cron 粒度限制**：GitHub cron 不支持「每 17 分钟」这类；分钟仅支持 `*/N`（N=1~59），小时为 `*/N`（N=1~24）。
- **GitHub schedule 是尽力而为**，实际触发会有 5~30 分钟抖动，不适合准实时。
- `/freq` 是真实改写并提交 `watch.yml`（commit message 含 `via Feishu`），可在提交历史里追溯。
- `GH_TOKEN` 会被用来改写仓库文件与变量，请用**专用** PAT；Vercel 环境变量仅存于服务端。
- 云端无法感知「本机是否已把最新批次下载到本地」，该状态请看本机 `output/` 与 `.last_pull.json`。
