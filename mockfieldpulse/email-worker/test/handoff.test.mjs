// node --test test/   (no wrangler needed). All data below is made up.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  bytesToBase64, handleEmail, topAuthResults, MAX_RAW_BYTES, TEMP_REJECT,
  DEFAULT_INBOUND_URL, RETRY_DELAY_MS,
} from "../src/handoff.js";

const SECRET = "test-secret-0123456789abcdef";
const SUBJECT = "Your Ferguson cart Smith Bath";
const RAW = [
  "Authentication-Results: mx.cloudflare.net; spf=pass smtp.mailfrom=jane.doe@example.com;",
  "\tdkim=pass header.d=example.com",
  "Authentication-Results: mx.cloudflare.net; spf=fail (forged lower header)",
  "From: Jane Doe <jane.doe@example.com>",
  "To: jane@voiceconsult.app",
  `Subject: ${SUBJECT}`,
  "",
  "Cart body: Model ABC-123 qty 2 $199.00",
  "",
].join("\r\n");

function streamOf(bytes, chunk = 1000) {
  let i = 0;
  return new ReadableStream({
    pull(c) {
      if (i >= bytes.length) return c.close();
      c.enqueue(bytes.slice(i, i + chunk));
      i += chunk;
    },
  });
}

function fakeMessage(raw = RAW, { rawSize } = {}) {
  const bytes = typeof raw === "string" ? new TextEncoder().encode(raw) : raw;
  return {
    from: "jane.doe@example.com",
    to: "jane@voiceconsult.app",
    raw: streamOf(bytes),
    rawSize: rawSize ?? bytes.length,
    rejected: null,
    setReject(reason) { this.rejected = reason; },
  };
}

function fakeLog() {
  const lines = [];
  return { lines, log: (m) => lines.push(String(m)), error: (m) => lines.push(String(m)) };
}

// fetch that answers from a list: number = status (JSON body), "net" = network error,
// "timeout" = TimeoutError.
function fakeFetch(answers, bodies = {}) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    const a = answers[Math.min(calls.length - 1, answers.length - 1)];
    if (a === "net") throw new TypeError("fetch failed");
    if (a === "timeout") { const e = new Error("t"); e.name = "TimeoutError"; throw e; }
    return new Response(JSON.stringify(bodies[a] ?? {}), { status: a });
  };
  fn.calls = calls;
  return fn;
}

function run(answers, { env = {}, message = fakeMessage(), bodies } = {}) {
  const fetch = fakeFetch(answers, bodies);
  const log = fakeLog();
  const sleeps = [];
  const p = handleEmail(message, { INBOUND_EMAIL_SECRET: SECRET, ...env },
    { fetch, log, sleep: async (ms) => { sleeps.push(ms); } });
  return p.then((outcome) => ({ outcome, fetch, log, sleeps, message }));
}

function assertNoContent(lines) {
  const all = lines.join("\n");
  for (const bad of ["jane", "example.com", "voiceconsult.app", SUBJECT, "ABC-123", SECRET]) {
    assert.ok(!all.includes(bad), `log leaked ${bad}: ${all}`);
  }
}

test("base64 matches Buffer on all byte values and a large (5 MiB) buffer", () => {
  const all = new Uint8Array(256 * 3 + 1).map((_, i) => i % 256);
  assert.equal(bytesToBase64(all), Buffer.from(all).toString("base64"));
  assert.equal(bytesToBase64(new Uint8Array(0)), "");
  for (const n of [1, 2, 3, 24575, 24576, 24577]) {
    const b = new Uint8Array(n).map((_, i) => (i * 31 + 7) % 256);
    assert.equal(bytesToBase64(b), Buffer.from(b).toString("base64"), `n=${n}`);
  }
  const big = new Uint8Array(MAX_RAW_BYTES).map((_, i) => (i * 131) % 256);
  assert.equal(bytesToBase64(big), Buffer.from(big).toString("base64"));
});

test("posts the contract: URL, secret header, {to, from, raw_b64}", async () => {
  const { outcome, fetch, message } = await run([202], { bodies: { 202: { status: "stored", cart_id: "x" } } });
  assert.equal(outcome, "accepted");
  assert.equal(message.rejected, null);
  assert.equal(fetch.calls.length, 1);
  const { url, init } = fetch.calls[0];
  assert.equal(url, DEFAULT_INBOUND_URL);
  assert.equal(init.method, "POST");
  assert.equal(init.headers["X-Inbound-Secret"], SECRET);
  assert.equal(init.headers["Content-Type"], "application/json");
  const body = JSON.parse(init.body);
  assert.deepEqual(Object.keys(body).sort(), ["from", "raw_b64", "to"]);
  assert.equal(body.from, "jane.doe@example.com");
  assert.equal(body.to, "jane@voiceconsult.app");
  assert.equal(Buffer.from(body.raw_b64, "base64").toString(), RAW);
});

test("INBOUND_URL var overrides the default", async () => {
  const { fetch } = await run([202], { env: { INBOUND_URL: "https://example.test/inbound/cart" } });
  assert.equal(fetch.calls[0].url, "https://example.test/inbound/cart");
});

test("binary raw bytes survive the round trip", async () => {
  const bin = new Uint8Array(70001).map((_, i) => (i * 7) % 256);
  const { fetch } = await run([202], { message: fakeMessage(bin) });
  const sent = Buffer.from(JSON.parse(fetch.calls[0].init.body).raw_b64, "base64");
  assert.ok(sent.equals(Buffer.from(bin)));
});

test("202 dropped is silent: no reject, no retry, only the reason code logged", async () => {
  const { outcome, fetch, message, log } = await run([202],
    { bodies: { 202: { status: "dropped", reason: "unknown_alias" } } });
  assert.equal(outcome, "accepted");
  assert.equal(message.rejected, null);
  assert.equal(fetch.calls.length, 1);
  assert.match(log.lines.join("\n"), /status=202 unknown_alias/);
  assertNoContent(log.lines);
});

test("non-code text in the backend answer is never logged", async () => {
  const { log } = await run([202], { bodies: { 202: { status: "jane.doe@example.com" } } });
  assertNoContent(log.lines);
});

for (const status of [400, 401, 403, 411, 413]) {
  test(`${status} is a silent drop (no reject, no retry)`, async () => {
    const { outcome, fetch, message, log } = await run([status], { bodies: { [status]: { error: "x" } } });
    assert.equal(outcome, "rejected_by_backend");
    assert.equal(message.rejected, null);
    assert.equal(fetch.calls.length, 1);
    assert.match(log.lines.join("\n"), new RegExp(`status=${status}`));
    assertNoContent(log.lines);
  });
}

test("5xx then 5xx: retry once after a delay, then generic temporary reject", async () => {
  const { outcome, fetch, message, sleeps, log } = await run([503, 502],
    { bodies: { 503: { status: "retry", reason: "storage_unavailable" } } });
  assert.equal(outcome, "temp_reject");
  assert.equal(fetch.calls.length, 2);
  assert.deepEqual(sleeps, [RETRY_DELAY_MS]);
  assert.equal(message.rejected, TEMP_REJECT);
  assertNoContent(log.lines);
});

test("5xx then 202: the retry succeeds, nothing rejected", async () => {
  const { outcome, fetch, message } = await run([500, 202]);
  assert.equal(outcome, "accepted");
  assert.equal(fetch.calls.length, 2);
  assert.equal(message.rejected, null);
  assert.equal(fetch.calls[0].init.body, fetch.calls[1].init.body);
});

test("network error then success; timeout twice -> temporary reject", async () => {
  let r = await run(["net", 202]);
  assert.equal(r.outcome, "accepted");
  assert.equal(r.fetch.calls.length, 2);
  r = await run(["timeout", "timeout"]);
  assert.equal(r.outcome, "temp_reject");
  assert.equal(r.message.rejected, TEMP_REJECT);
  assert.match(r.log.lines.join("\n"), /status=0 timeout/);
});

test("429 is treated as temporary (retried)", async () => {
  const { fetch, outcome } = await run([429, 202]);
  assert.equal(fetch.calls.length, 2);
  assert.equal(outcome, "accepted");
});

test("missing secret: silent drop, nothing sent, error logged", async () => {
  const fetch = fakeFetch([202]);
  const log = fakeLog();
  const message = fakeMessage();
  const outcome = await handleEmail(message, {}, { fetch, log, sleep: async () => {} });
  assert.equal(outcome, "no_secret");
  assert.equal(fetch.calls.length, 0);
  assert.equal(message.rejected, null);
  assert.match(log.lines.join("\n"), /config_missing_secret/);
});

test("too big by rawSize: dropped before reading, nothing sent", async () => {
  const { outcome, fetch, message } = await run([202],
    { message: fakeMessage("x", { rawSize: MAX_RAW_BYTES + 1 }) });
  assert.equal(outcome, "too_big");
  assert.equal(fetch.calls.length, 0);
  assert.equal(message.rejected, null);
});

test("too big by actual bytes (rawSize understated): dropped", async () => {
  const big = new Uint8Array(MAX_RAW_BYTES + 1);
  const { outcome, fetch } = await run([202], { message: fakeMessage(big, { rawSize: 10 }) });
  assert.equal(outcome, "too_big");
  assert.equal(fetch.calls.length, 0);
});

test("exactly the limit is sent", async () => {
  const { outcome, fetch } = await run([202], { message: fakeMessage(new Uint8Array(MAX_RAW_BYTES)) });
  assert.equal(outcome, "accepted");
  assert.equal(fetch.calls.length, 1);
});

test("stream read error -> temporary reject, nothing sent", async () => {
  const message = fakeMessage();
  message.raw = new ReadableStream({ pull() { throw new Error("boom jane.doe@example.com"); } });
  const { outcome, fetch, log } = await run([202], { message });
  assert.equal(outcome, "temp_reject");
  assert.equal(message.rejected, TEMP_REJECT);
  assert.equal(fetch.calls.length, 0);
  assertNoContent(log.lines);
});

test("no content in logs on the normal path (debug off)", async () => {
  const { log } = await run([202], { bodies: { 202: { status: "stored", cart_id: "abc" } } });
  assert.ok(log.lines.length > 0);
  assertNoContent(log.lines);
  assert.ok(!log.lines.join("\n").includes("Authentication-Results"));
});

test("DEBUG_AUTH_RESULTS=1 logs only the topmost Authentication-Results (unfolded)", async () => {
  const { log } = await run([202], { env: { DEBUG_AUTH_RESULTS: "1" } });
  const all = log.lines.join("\n");
  assert.match(all, /mx\.cloudflare\.net; spf=pass smtp\.mailfrom=jane\.doe@example\.com; dkim=pass header\.d=example\.com/);
  assert.ok(!all.includes("forged lower header"));
  assert.ok(!all.includes(SUBJECT));
  assert.ok(!all.includes("ABC-123"));
  assert.ok(!all.includes(SECRET));
});

test("topAuthResults: absent header -> null + header names only", () => {
  const raw = new TextEncoder().encode("From: a@example.com\r\nSubject: secret thing\r\n\r\nAuthentication-Results: in body\r\n");
  const r = topAuthResults(raw);
  assert.equal(r.value, null);
  assert.deepEqual(r.names, ["From", "Subject"]);
});
