# 飞书 → GitHub Actions 桥接（feishu-bridge）

让你在**飞书群**里直接发指令，控制 `media-batch-downloader` 的「博主更新监控」任务：
立即运行、调整频次、暂停/恢复、增删监控博主、查询状态。

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
| `/status` | 返回最近运行结果、监控对象、当前 cron、是否暂停 |

执行结果会用飞书机器人回执到群里。

## 架构

```
飞书群 @机器人 ──(HTTPS 回调)──> Vercel 函数 (/api/feishu)
                                    │ 解析指令
                                    ├─ 调 GitHub API（GH_TOKEN）
                                    │    · 触发 dispatch / 改 cron / 改 Variables
                                    └─ 回执飞书群（FEISHU_WEBHOOK）
```

## 一、飞书机器人配置（一次性）

1. 在飞书群「设置 → 群机器人 → 添加机器人（自定义）」。
2. 记下「机器人 webhook 地址」（`https://open.feishu.cn/open-apis/bot/v2/hook/xxx`）——
   这就是 `FEISHU_WEBHOOK`，也等于仓库里已有的 `NOTIFY_WEBHOOK`。
3. 开启「**接收消息**」，把**回调地址**填成你的 Vercel 部署地址 + `/api/feishu`
   （例如 `https://your-app.vercel.app/api/feishu`）。
4. 安全设置：**建议留空（不加签）**，与本仓库现有通知配置保持一致；
   若开启「签名校验」，请把「签名密钥」填到 `FEISHU_SIGN_SECRET`，函数会自动做入站校验并对出站回执加签。

> 首次保存回调地址时，飞书会发一次 `url_verification` 挑战，函数会自动回 `challenge` 通过校验。

## 二、本地调试

```bash
cp .env.example .env.local      # 填好 GH_TOKEN / FEISHU_WEBHOOK 等
npm i -g vercel                 # 或 npx vercel
vercel dev                      # 默认 http://localhost:3000
# 另开终端模拟飞书回调：
curl -X POST http://localhost:3000/api/feishu \
  -H 'Content-Type: application/json' \
  -d '{"schema":"2.0","header":{"event_type":"im.message.receive_v1"},"event":{"message":{"content":"{\"text\":\"@_user_1 /status\"}"}}}'
```

冒烟测试（mock 掉 GitHub/Feishu，验证指令分发）：

```bash
node test-local.mjs
```

## 三、Vercel 部署

```bash
cd feishu-bridge
vercel              # 按提示登录并部署；首部署会生成一个 .vercel 目录
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
| `FEISHU_WEBHOOK` | 飞书机器人发消息的 webhook 地址 |
| `FEISHU_SIGN_SECRET` | 可选；开启飞书加签时填，否则留空 |

改完环境变量**重新部署一次**使其生效。把生产域名下的 `/api/feishu` 填回飞书机器人「接收消息」回调地址即可。

## 注意事项

- **cron 粒度限制**：GitHub cron 不支持「每 17 分钟」这类；分钟指令仅支持 `*/N`（N=1~59），小时为 `*/N`（N=1~24）。
- **GitHub schedule 是尽力而为**，实际触发会有 5~30 分钟抖动，不适合准实时。
- `/freq` 是真实改写并提交 `watch.yml`（commit message 含 `via Feishu`），可在仓库提交历史里追溯。
- `GH_TOKEN` 会被用来改写仓库文件与变量，请用**专用** PAT，不要复用主账号高权限 token；Vercel 环境变量仅存于服务端，不会进前端代码。
- 抖音博主监控仍依赖 `DOUYIN_COOKIE` 变量，当前为空则抖音链接不会被实际下载。
