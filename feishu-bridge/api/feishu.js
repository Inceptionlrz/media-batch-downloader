// 飞书 → GitHub Actions 桥接（Vercel Node 函数）
// 接收飞书「接收消息」事件回调，解析指令后调用 GitHub API 控制 watch.yml 监控任务，再把结果回执到群。
//
// 支持指令（在群里 @机器人 发送）：
//   /run                  立即触发一次监控
//   /freq 2h              调整监控频次（2h / 6h / 12h / 30m …），改写 watch.yml 的 cron 并提交
//   /pause                暂停监控（置 WATCH_PAUSED=true）
//   /resume               恢复监控（清掉 WATCH_PAUSED）
//   /add @user            增加监控博主（追加进 WATCH_URLS 变量，支持 tiktok/douyin 主页）
//   /del @user            移除监控博主
//   /status               返回最近一次运行状态、配置与最近下载批次
//   /pull                 返回最近下载批次的直链与内容概要（本地用 scripts/pull_latest.py 同步）
//
// 设计要点（针对「飞书侧发起状态检测后无任何响应」的修复）：
//   1) 立即应答：飞书事件回调要求 3 秒内返回，否则判定失败且不展示任何回执。
//      本函数收到事件后立刻回 200，指令执行与回执放到 waitUntil() 后台继续跑。
//   2) 全链路超时：所有外部请求都带 AbortController 超时（默认 8s），避免无限挂起耗尽 30s maxDuration。
//   3) 失败可见：飞书 webhook 用 HTTP 200 + body.code!=0 表示业务失败，必须解析 body 才能发现；
//      这里对 GitHub / 飞书 token / 飞书回执全部记录「请求地址、耗时、响应码、响应体」日志，
//      并把最近错误写到内存缓冲，可通过 GET /api/feishu?diag=1 直接查看。
//   4) 回执双通道：优先用企业自建应用 API 定向回复（FEISHU_APP_ID/SECRET + tenant_access_token），
//      失败或未配置时降级到群自定义机器人 webhook（FEISHU_WEBHOOK）。
//
// 环境变量（Vercel 项目 Settings → Environment Variables）：
//   GH_TOKEN            GitHub PAT（需 repo 权限）
//   GH_OWNER            Inceptionlrz
//   GH_REPO             media-batch-downloader
//   GH_WORKFLOW         watch.yml
//   GH_BRANCH           main
//   FEISHU_WEBHOOK      群自定义机器人 webhook（bot/v2/hook/...），回执兜底通道
//   FEISHU_SIGN_SECRET  可选：机器人「加签」密钥；留空则不校验入站签名、出站也不带签名
//   FEISHU_APP_ID       可选：企业自建应用 App ID（启用定向回复，需 im:message 发送权限）
//   FEISHU_APP_SECRET   可选：企业自建应用 App Secret
//   FEISHU_BASE         可选：飞书域名，默认 https://open.feishu.cn（海外 Lark 改 https://open.larksuite.com）
//   HTTP_TIMEOUT_MS     可选：单次外部请求超时，默认 8000
//   LOG_FULL_URL        可选：置 1 时日志打印完整 URL（默认对 webhook/token 做脱敏）

import crypto from "node:crypto";

const OWNER = process.env.GH_OWNER;
const REPO = process.env.GH_REPO;
const WORKFLOW = process.env.GH_WORKFLOW || "watch.yml";
const BRANCH = process.env.GH_BRANCH || "main";
const GH_TOKEN = process.env.GH_TOKEN;
const FEISHU_WEBHOOK = process.env.FEISHU_WEBHOOK || "";
const SIGN_SECRET = process.env.FEISHU_SIGN_SECRET || "";
const FEISHU_APP_ID = process.env.FEISHU_APP_ID || "";
const FEISHU_APP_SECRET = process.env.FEISHU_APP_SECRET || "";
const FEISHU_BASE = (process.env.FEISHU_BASE || "https://open.feishu.cn").replace(/\/+$/, "");
const LOG_FULL_URL = process.env.LOG_FULL_URL === "1";
const HTTP_TIMEOUT_MS = Number(process.env.HTTP_TIMEOUT_MS || 8000);
// BRIDGE_SYNC=1 强制同步执行（本地测试/排障用，便于断言与观察完整耗时）
const FORCE_SYNC = process.env.BRIDGE_SYNC === "1";
const REGION = process.env.VERCEL_REGION || process.env.VERCEL_URL || "(unknown)";

const apiBase = "https://api.github.com";
const MAX_LOG_BODY = 500;
const MAX_TEXT = 8000; // 飞书单条文本消息上限较宽松，这里保守截断避免超限

// ---------- 运行期状态（用于诊断；进程复用期间有效，冷启动会清空） ----------
const state = {
  lastEvent: null,
  recentErrors: [],
  counters: { events: 0, replies: 0, replyFailures: 0 },
  startedAt: Date.now(),
};

function pushError(stage, message, extra) {
  const item = { at: new Date().toISOString(), stage, message, ...(extra || {}) };
  state.recentErrors.unshift(item);
  if (state.recentErrors.length > 20) state.recentErrors.length = 20;
}

// ---------- 日志 ----------
function log(level, msg, extra) {
  const line = extra ? `${msg} ${JSON.stringify(extra)}` : msg;
  const prefix = `[bridge][${level}]`;
  if (level === "error") console.error(prefix, line);
  else if (level === "warn") console.warn(prefix, line);
  else console.log(prefix, line);
}

// 脱敏：webhook 的 hook id 与 token 属于凭证，默认只保留域名与路径前缀
function redactUrl(u) {
  const s = String(u);
  if (LOG_FULL_URL) return s;
  return s
    .replace(/(\/hook\/)[^/?]+/, "$1***")
    .replace(/([?&](access_token|token|key|sign)=)[^&]+/g, "$1***");
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- 带超时/重试/日志的 fetch ----------
async function timedFetch(url, opts = {}, cfg = {}) {
  const {
    label = "http",
    timeoutMs = HTTP_TIMEOUT_MS,
    retries = 1,
    logBody = true,
  } = cfg;
  const method = (opts.method || "GET").toUpperCase();
  const shown = redactUrl(url);
  let lastErr = null;

  for (let attempt = 0; attempt <= retries; attempt++) {
    const t0 = Date.now();
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), timeoutMs);
    try {
      const res = await fetch(url, { ...opts, signal: ac.signal });
      const text = await res.text();
      const ms = Date.now() - t0;
      const extra = { status: res.status, ms };
      if (logBody) extra.body = text ? text.slice(0, MAX_LOG_BODY) : "";
      log(res.ok ? "info" : "warn", `${label} ${method} ${shown}`, extra);
      clearTimeout(timer);
      // 5xx 视为可重试；4xx 直接返回交给调用方处理
      if (res.status >= 500 && attempt < retries) {
        await sleep(300);
        continue;
      }
      return { res, text, ms, status: res.status };
    } catch (e) {
      const ms = Date.now() - t0;
      clearTimeout(timer);
      lastErr = e;
      const aborted = e && (e.name === "AbortError" || String(e.message).includes("abort"));
      log("warn", `${label} ${method} ${shown} ${aborted ? "超时" : "异常"}`, {
        ms,
        attempt,
        timeoutMs,
        err: String((e && e.message) || e),
      });
      if (attempt < retries) await sleep(300);
    }
  }
  throw lastErr || new Error(`${label} ${method} ${shown} 失败`);
}

function parseJsonSafe(text) {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
}

// ---------- GitHub ----------
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
  const url = apiBase + path;
  const { res, text } = await timedFetch(
    url,
    {
      method,
      headers: ghHeaders(body ? { "Content-Type": "application/json" } : {}),
      body: body ? JSON.stringify(body) : undefined,
    },
    { label: "github", retries: 0, timeoutMs: HTTP_TIMEOUT_MS }
  );
  const data = parseJsonSafe(text);
  if (!res.ok) {
    const msg = (data && (data.message || JSON.stringify(data))) || text || res.status;
    const err = new Error(`GitHub ${method} ${path} -> HTTP ${res.status}: ${String(msg).slice(0, 300)}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

// ---------- 飞书鉴权：tenant_access_token ----------
let tokenCache = { token: "", expireAt: 0 };

async function getTenantAccessToken() {
  if (!FEISHU_APP_ID || !FEISHU_APP_SECRET) {
    return null; // 未配置应用凭证 → 定向回复通道不可用，走 webhook 兜底
  }
  if (tokenCache.token && Date.now() < tokenCache.expireAt - 60 * 1000) {
    return tokenCache.token;
  }
  const url = `${FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal`;
  const { res, text } = await timedFetch(
    url,
    {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ app_id: FEISHU_APP_ID, app_secret: FEISHU_APP_SECRET }),
    },
    { label: "feishu.auth", retries: 0, logBody: false }
  );
  const data = parseJsonSafe(text) || {};
  if (!res.ok || data.code !== 0 || !data.tenant_access_token) {
    // 注意：飞书这里也是 HTTP 200 + code!=0，必须读 body 才能发现
    const msg = `获取 tenant_access_token 失败：HTTP ${res.status} code=${data.code} msg=${data.msg || text.slice(0, 200)}`;
    log("error", "feishu.auth", { status: res.status, code: data.code, msg: data.msg });
    pushError("feishu.auth", msg, { status: res.status, code: data.code });
    throw new Error(msg);
  }
  const expire = Number(data.expire || 7200);
  tokenCache = { token: data.tenant_access_token, expireAt: Date.now() + expire * 1000 };
  log("info", "feishu.auth ok", { expire });
  return tokenCache.token;
}

// ---------- 飞书回执 ----------
function clip(text) {
  return text.length > MAX_TEXT ? text.slice(0, MAX_TEXT) + "\n…（内容过长已截断）" : text;
}

// 通道 A：企业自建应用按 message_id 定向回复
async function replyViaApp(messageId, text) {
  if (!messageId) return { ok: false, channel: "app", reason: "事件缺少 message_id" };
  if (!FEISHU_APP_ID || !FEISHU_APP_SECRET) {
    return { ok: false, channel: "app", reason: "未配置 FEISHU_APP_ID / FEISHU_APP_SECRET" };
  }
  const token = await getTenantAccessToken();
  const url = `${FEISHU_BASE}/open-apis/im/v1/messages/${encodeURIComponent(messageId)}/reply`;
  const { res, text: rt } = await timedFetch(
    url,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json; charset=utf-8",
      },
      body: JSON.stringify({ msg_type: "text", content: JSON.stringify({ text: clip(text) }) }),
    },
    { label: "feishu.reply", retries: 0 }
  );
  const data = parseJsonSafe(rt) || {};
  if (!res.ok || data.code !== 0) {
    const msg = `定向回复失败：HTTP ${res.status} code=${data.code} msg=${data.msg || rt.slice(0, 200)}`;
    log("error", "feishu.reply", { status: res.status, code: data.code, msg: data.msg });
    pushError("feishu.reply", msg, { status: res.status, code: data.code });
    return { ok: false, channel: "app", reason: msg, code: data.code };
  }
  return { ok: true, channel: "app" };
}

// 通道 B：群自定义机器人 webhook（兜底）
async function replyViaWebhook(text) {
  if (!FEISHU_WEBHOOK) return { ok: false, channel: "webhook", reason: "未配置 FEISHU_WEBHOOK" };
  const body = { msg_type: "text", content: { text: clip(text) } };
  if (SIGN_SECRET) {
    const ts = String(Math.floor(Date.now() / 1000));
    body.timestamp = ts;
    // 飞书自定义机器人加签：HMAC-SHA256(key = "{ts}\n{secret}", msg = "") → base64
    body.sign = crypto.createHmac("sha256", `${ts}\n${SIGN_SECRET}`).digest("base64");
  }
  const { res, text: rt } = await timedFetch(
    FEISHU_WEBHOOK,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    { label: "feishu.webhook", retries: 0 }
  );
  const data = parseJsonSafe(rt) || {};
  // webhook 业务失败同样是 HTTP 200 + code!=0（如 19001 签名无效 / 9499 机器人不在群内）
  if (!res.ok || (data.code !== undefined && data.code !== 0)) {
    const msg = `webhook 回执失败：HTTP ${res.status} code=${data.code} msg=${data.msg || rt.slice(0, 200)}`;
    log("error", "feishu.webhook", { status: res.status, code: data.code, msg: data.msg });
    pushError("feishu.webhook", msg, { status: res.status, code: data.code });
    return { ok: false, channel: "webhook", reason: msg, code: data.code };
  }
  return { ok: true, channel: "webhook" };
}

// 组合回执：定向回复优先，失败降级 webhook，并把降级原因一并告知群里
async function sendReply(text, messageId) {
  const attempts = [];
  if (FEISHU_APP_ID && FEISHU_APP_SECRET) {
    try {
      const r = await replyViaApp(messageId, text);
      attempts.push(r);
      if (r.ok) {
        state.counters.replies++;
        log("info", "reply ok via app", { ms: 0 });
        return r;
      }
    } catch (e) {
      attempts.push({ ok: false, channel: "app", reason: String(e.message || e) });
      pushError("feishu.reply", String(e.message || e), {});
    }
  }
  try {
    const r = await replyViaWebhook(text);
    attempts.push(r);
    if (r.ok) {
      state.counters.replies++;
      if (attempts.length > 1) {
        const why = attempts[0].reason || "未知原因";
        await replyViaWebhook(`ℹ️ 说明：定向回复通道不可用（${why}），本次已改用群机器人回执。`).catch(() => {});
      }
      return r;
    }
  } catch (e) {
    attempts.push({ ok: false, channel: "webhook", reason: String(e.message || e) });
    pushError("feishu.webhook", String(e.message || e), {});
  }
  state.counters.replyFailures++;
  const summary = attempts.map((a) => `${a.channel}: ${a.reason}`).join(" | ");
  log("error", "所有回执通道均失败", { attempts });
  return { ok: false, channel: "none", reason: summary };
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
  }
  if (n < 1 || n > 59) throw new Error("分钟数需 1~59");
  return `*/${n} * * * *`;
}

// 改写 watch.yml 里的 cron 行（Git Data API：blob→tree→commit→ref）
async function setFreq(spec) {
  const cron = buildCron(spec);
  const ref = await gh("GET", `/repos/${OWNER}/${REPO}/git/ref/heads/${BRANCH}`);
  const baseSha = ref.object.sha;
  const commit = await gh("GET", `/repos/${OWNER}/${REPO}/git/commits/${baseSha}`);
  const baseTree = commit.tree.sha;
  const blobPath = `.github/workflows/${WORKFLOW}`;
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
  const human =
    cron === "23 * * * *" ? "每小时" : cron.startsWith("*/") ? `每 ${cron.match(/\d+/)[0]} 分钟` : `每 ${cron.match(/\*\/(\d+)/)[1]} 小时`;
  return `✅ 已将监控频次改为「${human}」（cron: ${cron}），已提交并推送。`;
}

async function getVar(name) {
  try {
    const d = await gh("GET", `/repos/${OWNER}/${REPO}/actions/variables/${name}`);
    return d.value;
  } catch (e) {
    const m = String(e.message || "");
    if (m.includes("404") || m.includes("Not Found")) return null;
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

function fmtTime(iso) {
  if (!iso) return "(未知)";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const bj = new Date(d.getTime() + 8 * 3600 * 1000);
  const p = (n) => String(n).padStart(2, "0");
  const bjStr = `${bj.getUTCFullYear()}-${p(bj.getUTCMonth() + 1)}-${p(bj.getUTCDate())} ${p(bj.getUTCHours())}:${p(bj.getUTCMinutes())}`;
  return `${iso.replace("T", " ").replace(/\.\d+Z$/, "Z")} / 北京 ${bjStr}`;
}

async function readCron() {
  try {
    const f = await gh("GET", `/repos/${OWNER}/${REPO}/contents/.github/workflows/${WORKFLOW}?ref=${BRANCH}`);
    const c = Buffer.from(f.content, "base64").toString("utf-8");
    return (c.match(/cron:\s*"([^"]*)"/) || [])[1] || "(未找到 cron)";
  } catch (e) {
    return `(读取失败: ${String(e.message).slice(0, 120)})`;
  }
}

async function latestBatch() {
  let rel = null;
  try {
    rel = await gh("GET", `/repos/${OWNER}/${REPO}/releases/latest`);
  } catch (e) {
    return `（读取最近批次失败：${String(e.message).slice(0, 160)}）`;
  }
  if (!rel) return "（暂无发布批次）";
  const assets = (rel.assets || []).map((a) => `${a.name} ${Math.round((a.size || 0) / 1024)}KB`).join("、");
  const dl = rel.assets && rel.assets[0] && rel.assets[0].browser_download_url;
  const lines = [
    `批次：${rel.tag_name}`,
    `发布时间：${fmtTime(rel.published_at)}`,
    `资源：${assets || "无"}`,
  ];
  if (dl) lines.push(`下载直链：${dl}`);
  if (rel.body) lines.push(`内容概要：\n${String(rel.body).trim().slice(0, 1200)}`);
  lines.push("本地是否已下载：云端无法感知，请在本机运行 scripts/pull_latest.py 同步并查看 .last_pull.json");
  return lines.join("\n");
}

async function status() {
  // 并行拉取，减少串行往返（飞书回调只有 3 秒预算）
  const [runsWrap, urlsWrap, pausedWrap, cron, rel] = await Promise.all([
    gh("GET", `/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/runs?per_page=1`).then(
      (v) => ({ v }),
      (e) => ({ e: String(e.message).slice(0, 160) })
    ),
    getVar("WATCH_URLS").then((v) => ({ v }), (e) => ({ e: String(e.message).slice(0, 160) })),
    getVar("WATCH_PAUSED").then((v) => ({ v }), (e) => ({ e: String(e.message).slice(0, 160) })),
    readCron(),
    gh("GET", `/repos/${OWNER}/${REPO}/releases/latest`).catch(() => null),
  ]);

  const r = (runsWrap.v && runsWrap.v.workflow_runs && runsWrap.v.workflow_runs[0]) || null;
  const runLine = r
    ? `最近运行：#${r.run_number} ${r.conclusion || r.status}（${fmtTime(r.created_at)}）\n运行直链：${r.html_url}`
    : `最近运行：读取失败（${runsWrap.e || "无运行记录"}）`;
  const urls = urlsWrap.v || (urlsWrap.e ? `(读取失败: ${urlsWrap.e})` : "(空)");
  const paused = pausedWrap.v === "true";

  let batch = "（读取失败）";
  if (rel) {
    const assets = (rel.assets || []).map((a) => `${a.name} ${Math.round((a.size || 0) / 1024)}KB`).join("、");
    const dl = rel.assets && rel.assets[0] && rel.assets[0].browser_download_url;
    batch = [`批次 ${rel.tag_name}｜发布 ${fmtTime(rel.published_at)}`, `资源：${assets || "无"}`];
    if (dl) batch.push(`直链：${dl}`);
    batch = batch.join("\n");
  }

  return [
    "📊 监控状态",
    runLine,
    `监控对象：\n  ${String(urls).split("\n").filter(Boolean).join("\n  ")}`,
    `频次 cron：${cron}`,
    `暂停：${paused ? "是（WATCH_PAUSED=true）" : "否"}`,
    "———— 最近下载批次 ————",
    batch,
  ].join("\n");
}

// ---------- 入站签名校验 ----------
function verifySignature(req, url) {
  if (!SIGN_SECRET) return { ok: true, skipped: "未配置 FEISHU_SIGN_SECRET" };
  const h = (k) => (req.headers && (req.headers[k] || req.headers[k.toLowerCase()])) || "";
  const ts = h("x-lark-request-timestamp") || url.searchParams.get("timestamp") || "";
  const nonce = h("x-lark-nonce") || "";
  const sign = h("x-lark-signature") || url.searchParams.get("sign") || "";
  if (!ts || !sign) return { ok: false, reason: "缺少 timestamp / signature" };
  // 飞书事件回调 v2 签名：base64(HMAC-SHA256(key = timestamp + nonce + secret, msg = ""))
  const expected = crypto.createHmac("sha256", `${ts}${nonce}${SIGN_SECRET}`).digest("base64");
  if (expected !== sign) return { ok: false, reason: "签名不匹配" };
  return { ok: true };
}

// ---------- 事件去重（飞书对超时/失败会重推，避免指令被重复执行） ----------
const seenEvents = new Map();
function isDuplicate(eventId) {
  if (!eventId) return false;
  const now = Date.now();
  for (const [k, t] of seenEvents) if (now - t > 10 * 60 * 1000) seenEvents.delete(k);
  if (seenEvents.has(eventId)) return true;
  seenEvents.set(eventId, now);
  return false;
}

// ---------- 后台执行（Vercel waitUntil；本地/不支持时退化为同步等待） ----------
async function runInBackground(promise) {
  if (!FORCE_SYNC) {
    try {
      const mod = await import("@vercel/functions");
      if (mod && typeof mod.waitUntil === "function") {
        mod.waitUntil(promise);
        return "waitUntil";
      }
    } catch {
      /* 未安装 → 退化为同步等待（仍可用，只是占用应答时间） */
    }
  }
  await promise;
  return FORCE_SYNC ? "sync-forced" : "sync-fallback";
}

// ---------- 诊断端点 ----------
async function diag(req, res) {
  const url = new URL(req.url, "http://localhost");
  const probe = url.searchParams.get("probe") || "";
  const out = {
    ok: true,
    service: "feishu-bridge",
    now: new Date().toISOString(),
    uptimeMs: Date.now() - state.startedAt,
    // 区域很关键：若函数区域远离中国大陆，回调往返耗时会吃掉飞书的 3 秒预算
    region: REGION,
    config: {
      GH_TOKEN: !!GH_TOKEN,
      GH_OWNER: OWNER || null,
      GH_REPO: REPO || null,
      GH_WORKFLOW: WORKFLOW,
      GH_BRANCH: BRANCH,
      FEISHU_WEBHOOK: FEISHU_WEBHOOK ? `${FEISHU_WEBHOOK.slice(0, 40)}…(len=${FEISHU_WEBHOOK.length})` : null,
      FEISHU_SIGN_SECRET: !!SIGN_SECRET,
      FEISHU_APP_ID: !!FEISHU_APP_ID,
      FEISHU_APP_SECRET: !!FEISHU_APP_SECRET,
      FEISHU_BASE,
      HTTP_TIMEOUT_MS,
    },
    replyChannels: {
      app: !!(FEISHU_APP_ID && FEISHU_APP_SECRET),
      webhook: !!FEISHU_WEBHOOK,
    },
    lastEvent: state.lastEvent,
    counters: state.counters,
    recentErrors: state.recentErrors,
    // 【需补充采集 / 无法在代码内自证的信息】见文件末尾 TODO 块
    missingInfo: [
      "飞书开放平台 → 事件订阅中是否已启用 im.message.receive_v1（需人工核对后台）",
      "是否已开通权限 im:message.group_at_msg:readonly（群 @ 机器人消息），且已重新发版生效",
      "回调地址是否为 https://fs.coldward.ccwu.cc/api/feishu（若填 *.vercel.app 会被国内 DNS 污染）",
      "群内是否同时存在：企业自建应用机器人（收指令）+ 群自定义机器人（发回执）",
      "若启用定向回复：应用是否具备 im:message / 发送消息权限",
    ],
  };

  if (probe.includes("github")) {
    const t0 = Date.now();
    try {
      const d = await gh("GET", `/repos/${OWNER}/${REPO}`);
      out.probeGithub = { ok: true, ms: Date.now() - t0, fullName: d.full_name };
    } catch (e) {
      out.probeGithub = { ok: false, ms: Date.now() - t0, error: String(e.message).slice(0, 300) };
    }
  }
  if (probe.includes("feishu")) {
    try {
      const r = await sendReply("🔧 feishu-bridge 自检：这是一条测试回执（diag probe）。", "");
      out.probeFeishu = r;
    } catch (e) {
      out.probeFeishu = { ok: false, error: String(e.message).slice(0, 300) };
    }
  }
  res.status(200).json(out);
}

// ---------- 主入口 ----------
export default async function handler(req, res) {
  const t0 = Date.now();
  const url = new URL(req.url, "http://localhost");

  // 诊断：GET /api/feishu 或 ?diag=1（可选 ?probe=github,feishu 做真实连通性探测）
  if ((req.method || "POST").toUpperCase() === "GET" || url.searchParams.get("diag") === "1") {
    try {
      return await diag(req, res);
    } catch (e) {
      log("error", "diag 失败", { err: String(e.message || e) });
      return res.status(500).json({ ok: false, error: String(e.message || e) });
    }
  }

  // 读取请求体（Vercel 可能已解析为对象，也可能是原始流）
  let payload = {};
  try {
    let raw = "";
    if (req.body && typeof req.body === "object" && !Buffer.isBuffer(req.body)) {
      payload = req.body;
    } else if (Buffer.isBuffer(req.body)) {
      raw = req.body.toString("utf-8");
      payload = raw ? JSON.parse(raw) : {};
    } else {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      raw = Buffer.concat(chunks).toString("utf-8");
      payload = raw ? JSON.parse(raw) : {};
    }
  } catch (e) {
    log("warn", "请求体解析失败（按空对象处理）", { err: String(e.message || e) });
    payload = {};
  }

  log("info", "inbound", {
    method: req.method,
    path: url.pathname,
    query: Object.fromEntries(url.searchParams),
    eventType: (payload.header && payload.header.event_type) || payload.type || "(none)",
    bodyLen: JSON.stringify(payload).length,
  });

  // 1) 握手校验（飞书首次填写回调地址时发 challenge）—— 必须在 3s 内返回
  const evt = (payload.header && payload.header.event_type) || payload.type || "";
  const challenge = payload.challenge || (payload.event && payload.event.challenge);
  if (evt === "url_verification" || payload.type === "url_verification" || challenge) {
    log("info", "url_verification", { challenge: String(challenge).slice(0, 40), ms: Date.now() - t0 });
    return res.status(200).json({ code: 0, challenge });
  }

  // 2) 可选签名校验
  const sig = verifySignature(req, url);
  if (!sig.ok) {
    log("warn", "签名校验失败", { reason: sig.reason });
    pushError("signature", sig.reason, {});
    return res.status(401).json({ code: 1, msg: sig.reason });
  }

  // 3) 去重
  const eventId = (payload.header && (payload.header.event_id || payload.header.eventId)) || "";
  if (isDuplicate(eventId)) {
    log("info", "重复事件已忽略", { eventId });
    return res.status(200).json({ code: 0, ignored: "duplicate" });
  }

  // 4) 提取文本与 message_id
  let text = "";
  let messageId = "";
  try {
    const msg = payload.event && payload.event.message;
    if (msg) {
      messageId = msg.message_id || msg.messageId || "";
      const c = typeof msg.content === "string" ? JSON.parse(msg.content) : msg.content;
      text = ((c && c.text) || "").toString();
    }
  } catch (e) {
    log("warn", "消息内容解析失败", { err: String(e.message || e) });
  }
  // 去除飞书提及占位符（@_user_1 / @_bot_1 / @_all）；用户命令里的 @用户名 是参数，不动
  text = text.replace(/@_(user|bot|all)_\w+/g, "").trim();

  const parts = text.split(/\s+/).filter(Boolean);
  const cmd = (parts[0] || "").toLowerCase();
  const arg = parts.slice(1).join(" ");

  state.counters.events++;
  state.lastEvent = {
    at: new Date().toISOString(),
    eventId,
    eventType: evt,
    chatId: (payload.event && payload.event.message && payload.event.message.chat_id) || "",
    messageId,
    cmd: cmd || "(空)",
    rawText: text.slice(0, 200),
  };

  // 5) 关键：立刻应答（飞书 3 秒超时），指令执行与回执放到后台
  if (state.lastEvent) state.lastEvent.ackMs = Date.now() - t0;
  const ack = { code: 0, accepted: true, cmd: cmd || "(空)" };
  res.status(200).json(ack);

  const mode = await runInBackground(
    (async () => {
      const t1 = Date.now();
      let result = "";
      if (!cmd) {
        result = "ℹ️ 未知指令。可用：/run /freq 2h /pause /resume /add @user /del @user /status /pull";
      } else {
        try {
          if (cmd === "/run") result = await triggerRun();
          else if (cmd === "/freq") result = await setFreq(arg || "6h");
          else if (cmd === "/pause") result = await pause();
          else if (cmd === "/resume") result = await resume();
          else if (cmd === "/add") result = arg ? await addUser(arg) : "用法：/add @用户名 或 /add https://...";
          else if (cmd === "/del") result = arg ? await delUser(arg) : "用法：/del @用户名";
          else if (cmd === "/status") result = await status();
          else if (cmd === "/pull") result = "📥 最新下载批次（用本地 pull_latest.py 同步到本机 output/）：\n" + (await latestBatch());
          else result = "ℹ️ 未知指令。可用：/run /freq 2h /pause /resume /add @user /del @user /status /pull";
        } catch (e) {
          result = "❌ 执行失败：" + String(e.message || e);
          log("error", "指令执行失败", { cmd, err: String(e.message || e) });
          pushError(cmd, String(e.message || e), {});
        }
      }
      log("info", "指令处理完成", { cmd, ms: Date.now() - t1, resultLen: result.length });

      const r = await sendReply(result, messageId);
      log(r.ok ? "info" : "error", "回执结果", { cmd, ok: r.ok, channel: r.channel, reason: r.reason || "", totalMs: Date.now() - t0 });
      if (state.lastEvent) {
        // ackMs 已在应答前写入；这里记录后台全部完成（含 GitHub 调用与回执）的耗时
        state.lastEvent.doneMs = Date.now() - t0;
        state.lastEvent.replyOk = r.ok;
        state.lastEvent.replyChannel = r.channel;
        state.lastEvent.replyError = r.ok ? "" : r.reason;
      }
    })()
  );
  log("info", "已应答", { mode, cmd, ackMs: Date.now() - t0 });

  return undefined;
}

// ============================================================================
// 【需补充采集 / 无法由代码自证的信息】（不要臆造，需使用者在飞书后台核对）
//
// 1. 事件订阅是否真正生效
//    - 后台路径：开发者后台 → 你的企业自建应用 → 事件订阅 → 已订阅事件列表是否含
//      「接收消息 im.message.receive_v1」，请求地址是否为 https://fs.coldward.ccwu.cc/api/feishu。
//    - 若填的是 *.vercel.app，国内 DNS 会被污染，回调根本到不了。
//
// 2. 权限是否开通并已发版
//    - 至少需要 im:message.group_at_msg:readonly（群 @ 机器人消息）。
//    - 改权限后必须重新「创建版本 → 发布」才会生效。
//
// 3. 群成员
//    - 群里必须同时有：企业自建应用机器人（收指令）+ 群自定义机器人（发回执）。
//      任一方被移出群，都会表现为「发了指令却毫无反应」。
//
// 4. 凭证是否配置（本函数只能判断「是否为空」，无法判断「是否有效」）
//    - FEISHU_WEBHOOK：是否仍为当前群的 webhook；换群/重建机器人后地址会变。
//    - FEISHU_APP_ID / FEISHU_APP_SECRET：若要启用定向回复需填，并给应用开通
//      「发送消息」相关权限（im:message），否则会返回业务错误码。
//
// 5. 若仍无响应，请采集以下信息再定位（每一项都能在下方 diag 或 Vercel Logs 里取到）
//    - GET https://fs.coldward.ccwu.cc/api/feishu?diag=1&probe=github,feishu
//      → config / region / lastEvent / recentErrors / probeGithub / probeFeishu
//    - Vercel 项目 → Logs：搜 [bridge]，含 inbound / 指令处理完成 / 回执结果 / 各请求耗时
//    - 飞书后台 → 事件订阅 → 事件推送记录：看是「未发送」「发送失败」还是「超时」
// ============================================================================
