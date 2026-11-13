// Ferguson cart email hand-off (stage E5). Pure module: no Cloudflare-only APIs, so
// `node --test` can run it. The Worker entry (index.js) only calls handleEmail().
//
// One job: take an email Cloudflare Email Routing gave us and POST it to our backend
// (POST /inbound/cart, see mockfieldpulse/voice-intake/carts_panel.receive_inbound):
//   header X-Inbound-Secret, JSON {to, from, raw_b64}, raw <= 5 MiB.
//
// Never bounce rejected mail (no alias probing). Outcomes:
//   2xx (stored / duplicate / dropped)          -> done, silent
//   other 4xx / 3xx (401, 400, 411, 413, ...)   -> silent drop, log the status only
//   5xx, 429, network error or timeout          -> wait, retry ONCE; still failing ->
//                                                  setReject(TEMP_REJECT) (generic text;
//                                                  unknown aliases also get 202 from the
//                                                  backend, so this leaks nothing)
//   too big                                     -> silent drop
//   INBOUND_EMAIL_SECRET missing (misconfig)    -> silent drop + error log (a config
//                                                  mistake must not bounce every email)
//   unexpected error reading the email          -> setReject(TEMP_REJECT)
//
// Logs: fixed codes, HTTP status and the backend's fixed status/reason code only. Never
// addresses, subject, content or the secret. DEBUG_AUTH_RESULTS="1" additionally logs the
// topmost Authentication-Results header (one-time check for E5; turn it off after).

export const MAX_RAW_BYTES = 5 * 1024 * 1024; // must match carts_panel.MAX_RAW_BYTES
export const DEFAULT_INBOUND_URL = "https://api.voiceconsult.app/inbound/cart";
export const TEMP_REJECT = "Temporary problem, please forward the cart again later.";
export const RETRY_DELAY_MS = 2000;
export const ATTEMPT_TIMEOUT_MS = 30000;

const LOG_PREFIX = "cart-email:";
const CODE_RE = /^[a-z_]{1,40}$/;

class TooBig extends Error {}

// Base64 of a Uint8Array, in chunks (String.fromCharCode.apply on 5 MB would blow the
// stack). Chunk size is a multiple of 3 so the pieces join without padding in between.
export function bytesToBase64(bytes) {
  const CHUNK = 3 * 8192;
  const parts = [];
  for (let i = 0; i < bytes.length; i += CHUNK) {
    parts.push(btoa(String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK))));
  }
  return parts.join("");
}

// Read a ReadableStream fully; throw TooBig as soon as it passes `limit` bytes.
export async function readAll(stream, limit) {
  const reader = stream.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    const chunk = value instanceof Uint8Array ? value : new Uint8Array(value);
    total += chunk.length;
    if (total > limit) {
      try { await reader.cancel(); } catch { /* ignore */ }
      throw new TooBig();
    }
    chunks.push(chunk);
  }
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) { out.set(c, off); off += c.length; }
  return out;
}

// The topmost Authentication-Results header (unfolded), or null. Also returns the header
// NAMES seen (no values) so a missing header can be diagnosed without logging content.
export function topAuthResults(bytes) {
  const slice = bytes.subarray(0, Math.min(bytes.length, 256 * 1024));
  let head = "";
  for (let i = 0; i < slice.length; i += 8192) {
    head += String.fromCharCode.apply(null, slice.subarray(i, i + 8192)); // byte-for-byte
  }
  const end = head.search(/\r?\n\r?\n/);
  const lines = (end >= 0 ? head.slice(0, end) : head).split(/\r?\n/);
  const fields = [];
  for (const line of lines) {
    if (/^[ \t]/.test(line) && fields.length) fields[fields.length - 1] += " " + line.trim();
    else fields.push(line);
  }
  const names = [];
  let found = null;
  for (const f of fields) {
    const colon = f.indexOf(":");
    if (colon <= 0) continue;
    const name = f.slice(0, colon).trim();
    names.push(name);
    if (found === null && name.toLowerCase() === "authentication-results") {
      found = f.slice(colon + 1).trim().slice(0, 2000);
    }
  }
  return { value: found, names };
}

function info(log, msg) { log.log(`${LOG_PREFIX} ${msg}`); }
function error(log, msg) { log.error(`${LOG_PREFIX} ${msg}`); }

const sleepMs = (ms) => new Promise((r) => setTimeout(r, ms));

// One POST. Returns {status, code} or {status: 0} for a network error / timeout.
async function postOnce(fetchFn, url, secret, body, timeoutMs) {
  try {
    const res = await fetchFn(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Inbound-Secret": secret },
      body,
      signal: AbortSignal.timeout(timeoutMs),
    });
    let code = "";
    try {
      const data = await res.json();
      const c = data && (data.reason || data.status);
      if (typeof c === "string" && CODE_RE.test(c)) code = c;
    } catch { /* body not JSON: status is enough */ }
    return { status: res.status, code };
  } catch (e) {
    return { status: 0, code: e && e.name === "TimeoutError" ? "timeout" : "network_error" };
  }
}

const retryable = (s) => s === 0 || s === 429 || s >= 500;

// Returns a fixed outcome code (for tests): "accepted" | "rejected_by_backend" | "too_big"
// | "no_secret" | "temp_reject".
export async function handleEmail(message, env, deps = {}) {
  const fetchFn = deps.fetch || globalThis.fetch;
  const sleep = deps.sleep || sleepMs;
  const log = deps.log || console;
  const timeoutMs = deps.timeoutMs || ATTEMPT_TIMEOUT_MS;

  const secret = env && env.INBOUND_EMAIL_SECRET;
  if (!secret) {
    error(log, "config_missing_secret (email dropped; set the Worker secret INBOUND_EMAIL_SECRET)");
    return "no_secret";
  }
  const url = (env && env.INBOUND_URL) || DEFAULT_INBOUND_URL;

  if (typeof message.rawSize === "number" && message.rawSize > MAX_RAW_BYTES) {
    info(log, "dropped too_big");
    return "too_big";
  }
  let bytes;
  try {
    bytes = await readAll(message.raw, MAX_RAW_BYTES);
  } catch (e) {
    if (e instanceof TooBig) {
      info(log, "dropped too_big");
      return "too_big";
    }
    error(log, "read_failed");
    message.setReject(TEMP_REJECT);
    return "temp_reject";
  }

  if (env.DEBUG_AUTH_RESULTS === "1") {
    const { value, names } = topAuthResults(bytes);
    if (value !== null) info(log, `debug Authentication-Results: ${value}`);
    else info(log, `debug no Authentication-Results header; header names: ${names.join(", ")}`);
  }

  const body = JSON.stringify({ to: message.to, from: message.from, raw_b64: bytesToBase64(bytes) });

  let r = await postOnce(fetchFn, url, secret, body, timeoutMs);
  if (retryable(r.status)) {
    info(log, `backend_unavailable status=${r.status}${r.code ? " " + r.code : ""}; retrying once`);
    await sleep(RETRY_DELAY_MS);
    r = await postOnce(fetchFn, url, secret, body, timeoutMs);
  }
  if (r.status >= 200 && r.status < 300) {
    info(log, `handed_off status=${r.status}${r.code ? " " + r.code : ""}`);
    return "accepted";
  }
  if (retryable(r.status)) {
    error(log, `backend_unavailable status=${r.status}${r.code ? " " + r.code : ""}; rejected as temporary`);
    message.setReject(TEMP_REJECT);
    return "temp_reject";
  }
  error(log, `backend_refused status=${r.status} (email dropped)`);
  return "rejected_by_backend";
}
