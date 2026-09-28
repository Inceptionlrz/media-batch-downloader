// 本地冒烟测试：模拟飞书回调，mock GitHub/Feishu 的 fetch，验证指令分发与回执。
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

const calls = [];
let replies = [];

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

  // 飞书回执
  if (u.startsWith(FEISHU_SENTINEL)) {
    replies.push(body && body.content && body.content.text);
    return jsonResp({ code: 0, msg: "success" });
  }
  // GitHub dispatches
  if (u.endsWith("/dispatches") && method === "POST") return jsonResp(null, 204);
  // runs list
  if (u.includes("/runs?per_page=1")) {
    return jsonResp({ workflow_runs: [{ run_number: 29, conclusion: "success", created_at: "2026-09-28T12:21:00Z" }] });
  }
  // variable GET
  if (/\/variables\/WATCH_URLS$/.test(u) && method === "GET") {
    return jsonResp({ name: "WATCH_URLS", value: "https://www.tiktok.com/@melanchui5" });
  }
  if (/\/variables\/WATCH_PAUSED$/.test(u) && method === "GET") {
    // 第一次 404（未设置），后续按我们写入的值
    const posted = calls.find((c) => c.method === "POST" && c.url.endsWith("/variables"));
    if (!posted) return jsonResp({ message: "Not Found" }, 404);
    const v = posted.body.value;
    return jsonResp({ name: "WATCH_PAUSED", value: v });
  }
  // variable POST/PATCH
  if (u.endsWith("/variables") && method === "POST") return jsonResp({ name: "WATCH_PAUSED", value: body.value });
  if (/\/variables\/WATCH_PAUSED$/.test(u) && method === "PATCH") return jsonResp({ name: "WATCH_PAUSED", value: body.value });
  if (/\/variables\/WATCH_URLS$/.test(u) && method === "PATCH") return jsonResp({ name: "WATCH_URLS", value: body.value });
  if (u.endsWith("/variables") && method === "PATCH") return jsonResp({ ok: true });
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

function makeReq(payload, query = "") {
  const body = JSON.stringify(payload);
  const req = Readable.from([Buffer.from(body)]);
  req.url = "/api/feishu" + query;
  req.method = "POST";
  return req;
}
function makeRes() {
  return {
    _status: 0, _body: null,
    status(c) { this._status = c; return this; },
    json(b) { this._body = b; return this; },
  };
}

async function send(payload, query = "") {
  calls.length = 0; replies = [];
  const req = makeReq(payload, query);
  const res = makeRes();
  await handler(req, res);
  return { status: res._status, body: res._body, reply: replies[0], calls };
}

const cases = [
  // 握手
  [{ type: "url_verification", challenge: "abc123" }, "", "握手"],
  // /run
  [buildMsg("/run"), "", "/run"],
  // /status
  [buildMsg("/status"), "", "/status"],
  // /pause
  [buildMsg("/pause"), "", "/pause"],
  // /resume
  [buildMsg("/resume"), "", "/resume"],
  // /add
  [buildMsg("/add @newbie"), "", "/add"],
  // /del
  [buildMsg("/del @newbie"), "", "/del"],
  // /freq
  [buildMsg("/freq 2h"), "", "/freq"],
  // 未知
  [buildMsg("/hello"), "", "未知指令"],
];

function buildMsg(text) {
  return {
    schema: "2.0",
    header: { event_type: "im.message.receive_v1" },
    event: { message: { content: JSON.stringify({ text: "@_user_1 " + text }) } },
  };
}

let pass = 0, fail = 0;
for (const [payload, q, label] of cases) {
  try {
    const r = await send(payload, q);
    const ok = r.status === 200;
    console.log(`[${ok ? "PASS" : "FAIL"}] ${label}  status=${r.status}  reply=${(r.reply || "").replace(/\n/g, " ⏎ ")}`);
    ok ? pass++ : fail++;
  } catch (e) {
    console.log(`[FAIL] ${label}  threw: ${e.message}`);
    fail++;
  }
}
console.log(`\n=== ${pass} passed, ${fail} failed ===`);
process.exit(fail ? 1 : 0);
