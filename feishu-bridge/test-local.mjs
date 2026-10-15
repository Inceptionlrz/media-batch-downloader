// 本地冒烟测试：模拟飞书回调，mock GitHub/Feishu 的 fetch，验证指令分发、回执、诊断与失败可见性。
// 运行：node test-local.mjs
import { Readable } from "node:stream";

const FEISHU_SENTINEL = "https://feishu.example/hook/test";
process.env.GH_TOKEN = "fake-token";
process.env.GH_OWNER = "Inceptionlrz";
process.env.GH_REPO = "media-batch-downloader";
process.env.GH_WORKFLOW = "watch.yml";
process.env.GH_BRANCH = "main";
process.env.FEISHU_WEBHOOK = FEISHU_SENTINEL;
process.env.FEISHU_SIGN_SECRET = "";
process.env.BRIDGE_SYNC = "1"; // 同步执行，便于断言

// 故障注入开关
let failWebhook = false; // 模拟飞书 webhook 返回 HTTP 200 + code!=0（业务失败）
let hangGithub = false; // 模拟 GitHub 请求挂起，验证超时控制

const calls = [];
let replies = [];

// mock 的仓库变量状态（让 /add → /del 形成闭环）
const vars = { WATCH_URLS: "https://www.tiktok.com/@melanchui5" };

function jsonResp(obj, status = 200) {
  return {
    ok: status < 400,
    status,
    text: async () => (obj == null ? "" : JSON.stringify(obj)),
    json: async () => obj,
  };
}

globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  const method = (opts.method || "GET").toUpperCase();
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({ method, url: u, body });

  if (hangGithub && u.startsWith("https://api.github.com")) {
    await new Promise((_, rej) => setTimeout(() => rej(Object.assign(new Error("aborted"), { name: "AbortError" })), 50));
  }

  // 飞书回执
  if (u.startsWith(FEISHU_SENTINEL)) {
    replies.push(body && body.content && body.content.text);
    if (failWebhook) return jsonResp({ code: 19001, msg: "sign invalid" });
    return jsonResp({ code: 0, msg: "success" });
  }
  // GitHub dispatches
  if (u.endsWith("/dispatches") && method === "POST") return jsonResp(null, 204);
  // runs list
  if (u.includes("/runs?per_page=1")) {
    return jsonResp({
      workflow_runs: [
        { run_number: 29, conclusion: "success", created_at: "2026-09-28T12:21:00Z", html_url: "https://github.com/x/actions/runs/29" },
      ],
    });
  }
  // releases/latest（/status 与 /pull 都依赖）
  if (u.includes("/releases/latest")) {
    return jsonResp({
      tag_name: "watch-42",
      published_at: "2026-10-12T15:18:00Z",
      assets: [{ name: "output-watch-42.zip", size: 26800000, browser_download_url: "https://example.com/output-watch-42.zip" }],
      body: "新增 10 条｜总计 12 文件 268.00MB",
    });
  }
  // 仓库元信息（diag 探测用）
  if (/\/repos\/Inceptionlrz\/media-batch-downloader$/.test(u)) {
    return jsonResp({ full_name: "Inceptionlrz/media-batch-downloader" });
  }
  // variable 读写（有状态，才能让 /add → /del 形成闭环）
  const varMatch = u.match(/\/variables\/([A-Z_]+)$/);
  if (u.includes("/actions/variables")) {
    const name = varMatch ? varMatch[1] : null;
    if (method === "GET" && name) {
      if (vars[name] === undefined) return jsonResp({ message: "Not Found" }, 404);
      return jsonResp({ name, value: vars[name] });
    }
    if ((method === "POST" || method === "PATCH") && body) {
      vars[body.name || name] = body.value;
      return jsonResp({ name: body.name || name, value: body.value });
    }
  }
  // contents (watch.yml)
  if (u.includes("/contents/") && method === "GET") {
    const yaml = 'name: x\non:\n  schedule:\n    - cron: "23 */6 * * *"\n';
    return jsonResp({ content: Buffer.from(yaml).toString("base64") });
  }
  // git data chain for /freq
  if (u.endsWith("/git/ref/heads/main") && method === "GET") return jsonResp({ object: { sha: "BASE" } });
  if (u.includes("/git/commits/BASE") && method === "GET") return jsonResp({ tree: { sha: "TREE" } });
  if (u.endsWith("/git/blobs") && method === "POST") return jsonResp({ sha: "BLOB" });
  if (u.endsWith("/git/trees") && method === "POST") return jsonResp({ sha: "TREE2" });
  if (u.endsWith("/git/commits") && method === "POST") return jsonResp({ sha: "COMMIT" });
  if (u.endsWith("/git/refs/heads/main") && method === "PATCH") return jsonResp({ ok: true });
  return jsonResp({ message: "unmocked " + method + " " + u }, 404);
};

const { default: handler } = await import("./api/feishu.js");

function makeReq(payload, query = "", method = "POST") {
  const body = method === "POST" ? JSON.stringify(payload) : "";
  const req = body ? Readable.from([Buffer.from(body)]) : Readable.from([]);
  req.url = "/api/feishu" + query;
  req.method = method;
  req.headers = {};
  return req;
}
function makeRes() {
  return {
    _status: 0,
    _body: null,
    status(c) {
      this._status = c;
      return this;
    },
    json(b) {
      this._body = b;
      return this;
    },
  };
}

async function send(payload, query = "", method = "POST") {
  calls.length = 0;
  replies = [];
  const req = makeReq(payload, query, method);
  const res = makeRes();
  await handler(req, res);
  return { status: res._status, body: res._body, reply: replies[0], calls, replies };
}

function buildMsg(text, eventId = "evt-" + Math.random().toString(36).slice(2)) {
  return {
    schema: "2.0",
    header: { event_id: eventId, event_type: "im.message.receive_v1" },
    event: { message: { message_id: "om_" + eventId, chat_id: "oc_test", content: JSON.stringify({ text: "@_user_1 " + text }) } },
  };
}

let pass = 0;
let fail = 0;
function check(label, cond, detail = "") {
  if (cond) {
    pass++;
    console.log(`[PASS] ${label}`);
  } else {
    fail++;
    console.log(`[FAIL] ${label} ${detail}`);
  }
}

const cases = [
  [{ type: "url_verification", challenge: "abc123" }, "", "握手", (r) => r.status === 200 && r.body.challenge === "abc123"],
  [buildMsg("/run"), "", "/run", (r) => r.status === 200 && /已触发/.test(r.reply || "")],
  [
    buildMsg("/status"),
    "",
    "/status 含批次与北京时间",
    (r) => r.status === 200 && /批次 watch-42/.test(r.reply || "") && /北京 /.test(r.reply || ""),
  ],
  [buildMsg("/pause"), "", "/pause", (r) => /已暂停/.test(r.reply || "")],
  [buildMsg("/resume"), "", "/resume", (r) => /已恢复/.test(r.reply || "")],
  [buildMsg("/add @newbie"), "", "/add", (r) => /已新增监控/.test(r.reply || "")],
  [buildMsg("/del @newbie"), "", "/del", (r) => /已移除/.test(r.reply || "")],
  [buildMsg("/freq 2h"), "", "/freq", (r) => /频次改为/.test(r.reply || "")],
  [buildMsg("/pull"), "", "/pull", (r) => /output-watch-42/.test(r.reply || "")],
  [buildMsg("/hello"), "", "未知指令", (r) => /未知指令/.test(r.reply || "")],
];

for (const [payload, q, label, assertFn] of cases) {
  try {
    const r = await send(payload, q);
    check(label, assertFn(r), `status=${r.status} reply=${(r.reply || "").slice(0, 120)}`);
  } catch (e) {
    check(label, false, `threw: ${e.message}`);
  }
}

// 1) 重复事件去重
{
  const p = buildMsg("/run", "evt-dup-1");
  await send(p);
  const r2 = await send(p);
  check("重复事件被忽略", r2.status === 200 && r2.body && r2.body.ignored === "duplicate", JSON.stringify(r2.body));
}

// 2) 飞书 webhook 业务失败（HTTP 200 + code!=0）必须被记录为可见错误
{
  failWebhook = true;
  const r = await send(buildMsg("/run", "evt-fail-1"));
  failWebhook = false;
  check("webhook 业务失败时仍回 200（不阻塞飞书）", r.status === 200, `status=${r.status}`);

  const d = await send(null, "?diag=1", "GET");
  const errs = (d.body && d.body.recentErrors) || [];
  check(
    "webhook 业务失败写入 recentErrors（失败可见）",
    errs.some((e) => e.stage === "feishu.webhook" && /19001/.test(e.message)),
    JSON.stringify(errs.slice(0, 2))
  );
  check("replyFailures 计数增加", (d.body.counters.replyFailures || 0) >= 1, JSON.stringify(d.body.counters));
}

// 3) GET 诊断端点
{
  const d = await send(null, "?diag=1&probe=github", "GET");
  check("diag 返回配置与区域", d.status === 200 && d.body.config && "region" in d.body, JSON.stringify(d.body).slice(0, 200));
  check("diag 的 GitHub 探测成功", d.body.probeGithub && d.body.probeGithub.ok === true, JSON.stringify(d.body.probeGithub));
  check("diag 列出需人工核对的信息", Array.isArray(d.body.missingInfo) && d.body.missingInfo.length > 0);
}

// 4) GitHub 挂起时必须被超时打断（不无限等待）
{
  hangGithub = true;
  const t0 = Date.now();
  const r = await send(buildMsg("/status", "evt-timeout-1"));
  const ms = Date.now() - t0;
  hangGithub = false;
  check("GitHub 超时被捕获并回执错误", r.status === 200 && /执行失败|超时|aborted/.test(r.reply || ""), `reply=${(r.reply || "").slice(0, 120)}`);
  check(`超时耗时受控（${ms}ms < 30s）`, ms < 30000, `ms=${ms}`);
}

console.log(`\n=== ${pass} passed, ${fail} failed ===`);
process.exit(fail ? 1 : 0);
