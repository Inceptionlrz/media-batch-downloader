#!/bin/bash
# feishu-bridge 一键部署：创建 Vercel 项目 → 写入环境变量 → 生产部署
#
# 用法（token 不写进代码，只从环境变量读取）：
#   export VERCEL_TOKEN="你的 token（https://vercel.com/account/tokens 创建）"
#   bash deploy.sh
#
# 说明：
#   1) 首次 `vercel` 会创建项目（默认取目录名 feishu-bridge）并生成 .vercel/project.json；
#   2) 环境变量必须在部署后写入，再重新部署一次才会注入到函数运行时 —— 所以是「部署→写变量→再部署」。

set -euo pipefail

: "${VERCEL_TOKEN:?请先 export VERCEL_TOKEN=... （到 https://vercel.com/account/tokens 创建）}"

cd "$(dirname "$0")"

# GitHub PAT：复用本机已存的那份（跨任务通用），避免手工粘贴
GH_PAT="$(tr -d '\r\n' < /c/Users/samal/.workbuddy/github_token.txt 2>/dev/null || true)"
if [ -z "${GH_PAT}" ]; then
  echo "错误：读不到 GitHub PAT（/c/Users/samal/.workbuddy/github_token.txt）" >&2
  exit 1
fi
FEISHU_HOOK="https://open.feishu.cn/open-apis/bot/v2/hook/a71f35f0-2b1d-4dad-9ad7-e7576ac339a3"

set_env() {
  local key="$1" val="$2"
  # 幂等：先尝试删除（不存在会失败，忽略），再写入
  vercel env rm "$key" production --yes --token "$VERCEL_TOKEN" >/dev/null 2>&1 || true
  printf '%s' "$val" | vercel env add "$key" production --token "$VERCEL_TOKEN" >/dev/null
  echo "  ✓ $key"
}

echo "==> 1/3 首次部署（创建项目）"
vercel --token "$VERCEL_TOKEN" --yes

echo "==> 2/3 写入环境变量"
set_env GH_TOKEN        "$GH_PAT"
set_env GH_OWNER        "Inceptionlrz"
set_env GH_REPO         "media-batch-downloader"
set_env GH_WORKFLOW     "watch.yml"
set_env GH_BRANCH       "main"
set_env FEISHU_WEBHOOK  "$FEISHU_HOOK"

echo "==> 3/3 生产部署（使环境变量生效）"
URL="$(vercel --prod --token "$VERCEL_TOKEN" --yes 2>&1 | tail -5)"
echo "$URL"
echo ""
echo "完成。把上面的生产域名 + /api/feishu 填到飞书机器人「接收消息」回调地址即可。"
echo "例如：https://feishu-bridge.vercel.app/api/feishu"
