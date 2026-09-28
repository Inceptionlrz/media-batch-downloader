// 飞书 → GitHub Actions 桥接（Vercel Node 函数，零依赖，仅用 Node 内置 fetch/crypto）
// 接收飞书群机器人「接收消息」回调，解析指令后调用 GitHub API 控制 watch.yml 监控任务。
//
// 支持指令（在群里 @机器人 发送）：
//   /run                  立即触发一次监控
//   /freq 2h              调整监控频次（2h / 6h / 12h / 30m …），改写 watch.yml 的 cron 并提交
//   /pause                暂停监控（置 WATCH_PAUSED=true）
//   /resume               恢复监控（清掉 WATCH_PAUSED）
//   /add @user            增加监控博主（追加进 WATCH_URLS 变量，支持 tiktok/douyin 主页）
//   /del @user            移除监控博主
//   /status               返回最近一次运行状态与配置
//
import crypto from "node:crypto";

// 环境变量（在 Vercel 项目 Settings → Environment Variables 配置）：
//   GH_TOKEN          GitHub PAT（需 repo 权限）
//   GH_OWNER          Inceptionlrz
//   GH_REPO           media-batch-downloader
//   GH_WORKFLOW       watch.yml
//   GH_BRANCH         main
//   FEISHU_WEBHOOK    飞书机器人发消息的 webhook（bot/v2/hook/...），用于回执
//   FEISHU_SIGN_SECRET  可选：飞书机器人「加签」密钥；留空则不校验签名

const OWNER = process.env.GH_OWNER;
const REPO = process.env.GH_REPO;
const WORKFLOW = process.env.GH_WORKFLOW || "watch.yml";
const BRANCH = process.env.GH_BRANCH || "main";
const GH_TOKEN = process.env.GH_TOKEN;
const FEISHU_WEBHOOK = process.env.FEISHU_WEBHOOK;
const SIGN_SECRET = process.env.FEISHU_SIGN_SECRET || "";

const apiBase = "https://api.github.com";

function ghHeaders(extra = {}) {
  return {
    Authorization: `Bearer ${GH_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "feishu-bridge",
    ...extra,
  };
}

async function gh(method, path, body) {
  const res = await fetch(apiBase + path, {
    method,
    headers: ghHeaders(body ? { "Content-Type": "application/json" } : {}),
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch {}
  if (!res.ok) {
    const msg = (data && (data.message || JSON.stringify(data))) || text || res.status;
    throw new Error(`GitHub ${method} ${path} -> ${res.status}: ${msg}`);
  }
  return data;
}

// ---------- 指令实现 ----------

async function triggerRun() {
  await gh("POST", `/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, { ref: BRANCH });
  return "✅ 已触发一次监控运行（watch.yml），稍后飞书会收到结果通知。";
}

function buildCron(spec) {
  const m = String(spec).trim().toLowerCase().match(/^(\d+)\s*(h|m|hour|min)?$/);
  if (!m) throw new Error("格式应为 /freq 2h 或 /freq 30m");
  const n = parseInt(m[1], 10);
  const unit = (m[2] || "h")[0];
  if (unit === "h") {
    if (n < 1 || n > 24) throw new Error("小时数需 1~24");
    return n === 1 ? "23 * * * *" : `23 */${n} * * *`;
  } else {
    if (n < 1 || n > 59) throw new Error("分钟数需 1~59");
    return `*/${n} * * * *`;
  }
}

// 改写 watch.yml 里的 cron 行（Git Data API：blob→tree→commit→ref）
async function setFreq(spec) {
  const cron = buildCron(spec);
  const ref = await gh("GET", `/repos/${OWNER}/${REPO}/git/ref/heads/${BRANCH}`);
  const baseSha = ref.object.sha;
  const commit = await gh("GET", `/repos/${OWNER}/${REPO}/git/commits/${baseSha}`);
  const baseTree = commit.tree.sha;
  const blobPath = `.github/workflows/${WORKFLOW}`;
  const oldBlob = await gh("GET", `/repos/${OWNER}/${REPO}/git/blobs/${commit.sha}`, null)
    .catch(() => null);
  // 直接取文件内容（contents API 返回 base64）
  const file = await gh("GET", `/repos/${OWNER}/${REPO}/contents/${blobPath}?ref=${BRANCH}`);
  const content = Buffer.from(file.content, "base64").toString("utf-8");
  if (!/cron:\s*"/.test(content)) throw new Error("未在 watch.yml 中找到 cron 定义");
  const updated = content.replace(/cron:\s*"[^"]*"/, `cron: "${cron}"`);
  if (updated === content) throw new Error("cron 内容未变化，无需提交");
  const newBlob = await gh("POST", `/repos/${OWNER}/${REPO}/git/blobs`, {
    content: Buffer.from(updated).toString("base64"),
    encoding: "base64",
  });
  const newTree = await gh("POST", `/repos/${OWNER}/${REPO}/git/trees`, {
    base_tree: baseTree,
    tree: [{ path: blobPath, mode: "100644", type: "blob", sha: newBlob.sha }],
  });
  const newCommit = await gh("POST", `/repos/${OWNER}/${REPO}/git/commits`, {
    message: `chore(watch): adjust schedule to ${cron} via Feishu`,
    tree: newTree.sha,
    parents: [baseSha],
  });
  await gh("PATCH", `/repos/${OWNER}/${REPO}/git/refs/heads/${BRANCH}`, { sha: newCommit.sha });
  const human = cron === "23 * * * *" ? "每小时" : cron.startsWith("*/")
    ? `每 ${cron.match(/\d+/)[0]} 分钟` : `每 ${cron.match(/\*\/(\d+)/)[1]} 小时`;
  return `✅ 已将监控频次改为「${human}」（cron: ${cron}），已提交并推送。`;
}

async function getVar(name) {
  try {
    const d = await gh("GET", `/repos/${OWNER}/${REPO}/actions/variables/${name}`);
    return d.value;
  } catch (e) {
    if (String(e.message).includes("404") || String(e.message).includes("Not Found")) return null;
    throw e;
  }
}

async function setVar(name, value) {
  const existing = await getVar(name);
  if (existing === null) {
    await gh("POST", `/repos/${OWNER}/${REPO}/actions/variables`, { name, value });
  } else {
    await gh("PATCH", `/repos/${OWNER}/${REPO}/actions/variables/${name}`, { value });
  }
}

async function pause() {
  await setVar("WATCH_PAUSED", "true");
  return "⏸️ 已暂停监控（WATCH_PAUSED=true）。发送 /resume 恢复。";
}

async function resume() {
  await setVar("WATCH_PAUSED", "false");
  return "▶️ 已恢复监控。";
}

function normalizeHandle(arg) {
  let s = String(arg).trim();
  s = s.replace(/^@/, "");
  if (/^https?:\/\//.test(s)) return s.replace(/\/+$/, "");
  return "https://www.tiktok.com/@" + s;
}

async function addUser(arg) {
  const url = normalizeHandle(arg);
  const cur = (await getVar("WATCH_URLS")) || "";
  const lines = cur.split("\n").map((x) => x.trim()).filter(Boolean);
  if (lines.some((l) => l === url || l.endsWith("@" + url.split("@").pop()))) {
    return `ℹ️ 已在监控列表中：${url}`;
  }
  lines.push(url);
  await setVar("WATCH_URLS", lines.join("\n"));
  return `✅ 已新增监控：${url}\n当前共 ${lines.length} 个：` + lines.map((l) => "\n  - " + l).join("");
}

async function delUser(arg) {
  const url = normalizeHandle(arg);
  const cur = (await getVar("WATCH_URLS")) || "";
  const lines = cur.split("\n").map((x) => x.trim()).filter(Boolean);
  const key = url.split("@").pop();
  const next = lines.filter((l) => !l.endsWith("@" + key) && l !== url);
  if (next.length === lines.length) return `ℹ️ 监控列表中没有：${url}`;
  await setVar("WATCH_URLS", next.join("\n"));
  return `✅ 已移除：${url}\n剩余 ${next.length} 个：` + next.map((l) => "\n  - " + l).join("");
}

async function status() {
  const runs = await gh("GET", `/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/runs?per_page=1`);
  const r = runs.workflow_runs[0];
  const urls = (await getVar("WATCH_URLS")) || "(空)";
  const paused = (await getVar("WATCH_PAUSED")) === "true";
  let cron = "(读取失败)";
  try {
    const f = await gh("GET", `/repos/${OWNER}/${REPO}/contents/.github/workflows/${WORKFLOW}?ref=${BRANCH}`);
    const c = Buffer.from(f.content, "base64").toString("utf-8");
    cron = (c.match(/cron:\s*"([^"]*)"/) || [])[1] || cron;
  } catch {}
  return [
    "📊 监控状态",
    `最近运行：#${r.run_number} ${r.conclusion || r.status}（${r.created_at}）`,
    `监控对象：\n  ${urls.split("\n").filter(Boolean).join("\n  ")}`,
    `频次 cron：${cron}`,
    `暂停：${paused ? "是（WATCH_PAUSED=true）" : "否"}`,
  ].join("\n");
}

// ---------- 飞书收发 ----------

async function reply(text) {
  if (!FEISHU_WEBHOOK) return;
  const body = { msg_type: "text", content: { text } };
  if (SIGN_SECRET) {
    const ts = String(Math.floor(Date.now() / 1000));
    const sig = crypto
      .createHmac("sha256", `${ts}\n${SIGN_SECRET}`)
      .digest("base64");
    body.timestamp = ts;
    body.sign = sig;
  }
  await fetch(FEISHU_WEBHOOK, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function genSign(timestamp) {
  return crypto
    .createHmac("sha256", `${timestamp}\n${SIGN_SECRET}`)
    .digest("base64");
}

// ---------- 主入口 ----------

export default async function handler(req, res) {
  const url = new URL(req.url, "http://localhost");
  const ts = url.searchParams.get("timestamp");
  const sign = url.searchParams.get("sign");

  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks).toString("utf-8");
  let payload = {};
  try { payload = raw ? JSON.parse(raw) : {}; } catch {}

  // 1) 握手校验（飞书首次填写回调地址时会发 challenge）
  const evt = payload.header && payload.header.event_type;
  const challenge = payload.challenge || (payload.event && payload.event.challenge);
  if (evt === "url_verification" || (payload.type === "url_verification")) {
    return res.status(200).json({ code: 0, challenge });
  }

  // 2) 可选签名校验
  if (SIGN_SECRET && ts && sign) {
    const expected = genSign(ts);
    if (expected !== sign) {
      return res.status(401).json({ code: 1, msg: "invalid sign" });
    }
  } else if (SIGN_SECRET && (!ts || !sign)) {
    return res.status(401).json({ code: 1, msg: "missing sign" });
  }

  // 3) 提取文本
  let text = "";
  try {
    const msg = payload.event && payload.event.message;
    if (msg && msg.content) {
      const c = typeof msg.content === "string" ? JSON.parse(msg.content) : msg.content;
      text = (c.text || "").toString();
    }
  } catch {}
  // 去除飞书提及占位符（如 @_user_1 / @_bot_1 / @_all）；用户命令里的 @用户名 是参数，不动
  text = text.replace(/@_(user|bot|all)_\w+/g, "").trim();

  // 4) 解析指令
  const parts = text.split(/\s+/).filter(Boolean);
  const cmd = (parts[0] || "").toLowerCase();
  const arg = parts.slice(1).join(" ");

  let result = "ℹ️ 未知指令。可用：/run /freq 2h /pause /resume /add @user /del @user /status";
  if (cmd) {
    try {
      if (cmd === "/run") result = await triggerRun();
      else if (cmd === "/freq") result = await setFreq(arg || "6h");
      else if (cmd === "/pause") result = await pause();
      else if (cmd === "/resume") result = await resume();
      else if (cmd === "/add") result = arg ? await addUser(arg) : "用法：/add @用户名 或 /add https://...";
      else if (cmd === "/del") result = arg ? await delUser(arg) : "用法：/del @用户名";
      else if (cmd === "/status") result = await status();
    } catch (e) {
      result = "❌ 执行失败：" + e.message;
    }
  }

  await reply(result);
  return res.status(200).json({ code: 0 });
}
