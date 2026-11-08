// What may leave the browser for Sentry (F-31..F-37). Pure functions, no
// imports, so `node --test src/lib/sentryPrivacy.test.mjs` covers them; only
// lib/sentry.jsx uses them (beforeSend / beforeSendTransaction /
// beforeBreadcrumb / the transport wrapper).
//
// Policy: every payload is REBUILT from an allowlist; each kept field must pass
// a value rule (enum, id/number shape, URL without userinfo/query, or a fixed
// template). A field without a rule is dropped. No free text is ever kept:
//  - Envelope: only event / transaction / session(s) / client_report items go
//    out (span streaming, logs, replays, profiles, attachments are dropped);
//    the envelope `trace` header (dynamic sampling context) is rebuilt from
//    trace_id / public_key / sample_rate / sample_rand / sampled /
//    environment / release only.
//  - Exceptions: value kept only when it is one of our fixed messages, a known
//    browser message rebuilt as a template ("Cannot read properties of null
//    (reading '[redacted]')"), or a known prefix + [redacted]. Type must be a
//    known built-in/DOM/Clerk error name (else "Error"). Frames keep code
//    location only (function, file URL, line/col).
//  - Breadcrumbs: known categories only; console dropped; message dropped;
//    fetch/xhr/navigation keep method, URL, status.
//  - Contexts: only `trace` (ids, op, status, origin, validated data).
//    Tags, extra, user, message text, logentry are dropped.
//  - Spans/transactions: op / origin / status validated, description rebuilt
//    from op ("GET <url>", resource URL, page path) else dropped, data by rule.
//  - URLs (F-38): page URLs keep only known app path words (else "[id]") and
//    the #admin / #consultations / #documents route word + "[id]"; requests to our API become
//    endpoint templates ("/job-status/[id]"); bundle assets keep their path;
//    anything else is origin only. Never userinfo or query.
//  - Data keys are an exact list (F-39); numbers only for counts, durations,
//    status codes, each bounded.
//  - Sessions are rebuilt with our own release/environment (F-40); `sdk` is
//    rebuilt from our constants (F-41), in events and envelope headers.

export const REDACTED = '[redacted]'

// ---- value rules -------------------------------------------------------------

const HTTP_METHODS = new Set(['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])
const LEVELS = new Set(['fatal', 'error', 'warning', 'log', 'info', 'debug'])
const SPAN_STATUS = new Set([
  'ok', 'cancelled', 'unknown', 'invalid_argument', 'deadline_exceeded', 'not_found',
  'already_exists', 'permission_denied', 'resource_exhausted', 'failed_precondition', 'aborted',
  'out_of_range', 'unimplemented', 'internal_error', 'unavailable', 'data_loss', 'unauthenticated',
  'error',
])
const OP = /^(?:pageload|navigation|http|resource|browser|ui|mark|measure|paint|function|db|cache|queue|middleware|react|default)(?:\.[a-z_]{1,24}(?:-[a-z_]{1,24})?){0,4}$/
const ORIGIN = /^(?:auto|manual)(?:\.[a-z_]{1,24}){0,5}$/
const SOURCES = new Set(['url', 'route', 'view', 'component', 'task', 'custom'])
const HEX32 = /^[0-9a-f]{32}$/
const HEX16 = /^[0-9a-f]{16}$/
const UUID = /^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$/i
const HOST = /^[a-z0-9.-]{1,253}(?::\d{1,5})?$/i
const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})$/

const isNum = (v) => typeof v === 'number' && Number.isFinite(v)
const oneOf = (set) => (v) => (typeof v === 'string' && set.has(v) ? v : undefined)
const shape = (re) => (v) => (typeof v === 'string' && re.test(v) ? v : undefined)
const num = (v) => (isNum(v) ? v : undefined)
const bool = (v) => (typeof v === 'boolean' ? v : undefined)
const between = (lo, hi, int) => (v) => (isNum(v) && v >= lo && v <= hi && (!int || Number.isInteger(v)) ? v : undefined)
const statusCode = between(0, 599, true) // 0 = network error (fetch/xhr)
const count = between(0, 1e7, true)
const bytes = between(0, 2 ** 32, true)
const duration = between(0, 1e9, false) // ms (or s), from performance timing

// ---- URLs (F-38) ---------------------------------------------------------------

export const ID = '[id]'
// Path words of the app's own pages (served from /). Anything else -> [id].
const APP_PATH_WORDS = new Set(['index.html'])
// Our API (lib/api.js): ':id' is one dynamic segment.
const API_ROUTES = [
  '/health', '/app/customers', '/consultation/upload-url', '/consultation/submit', '/jobs/recent',
  '/job-status/:id', '/admin/consultations', '/admin/consultations/:id',
  '/admin/consultations/:id/send', '/admin/consultations/:id/audio-url',
  // Vendor documents (stage P1b); the fixed words before '/documents/:id'.
  // Stage P2 (per customer) BEFORE '/documents/:id', which would also match 'customers'.
  '/documents/customers', '/documents/customers/:id', '/documents/customers/:id/send',
  '/documents', '/documents/upload-url', '/documents/submit', '/documents/:id',
].map((t) => t.split('/').slice(1))
// Built bundle files (prod /assets/*) and Vite dev module paths: code, not data.
const CODE_PATH = /^\/(?:assets\/[\w.-]{1,120}\.(?:m?js|css)|src\/[\w./-]{1,200}\.(?:m?jsx?|tsx?|css)|node_modules\/[\w./@-]{1,200}\.m?js|@vite\/client|@react-refresh)$/

function parseUrl(value) {
  if (typeof value !== 'string') return undefined
  const text = value.trim()
  try {
    if (/^https?:\/\//i.test(text)) {
      const u = new URL(text)
      return { origin: `${u.protocol}//${u.host}`, path: u.pathname, hash: u.hash } // host: no userinfo
    }
    if (text.startsWith('/') && !text.startsWith('//')) {
      const u = new URL(text, 'http://relative.invalid')
      return { origin: '', path: u.pathname, hash: u.hash }
    }
  } catch (_) {
    return undefined
  }
  return undefined
}

function apiTemplate(path) {
  const segs = path.split('/').slice(1)
  const route = API_ROUTES.find((r) => r.length === segs.length
    && r.every((part, i) => (part === ':id' ? segs[i] !== '' : part === segs[i])))
  return route ? '/' + route.map((part) => (part === ':id' ? ID : part)).join('/') : undefined
}

// #admin, #admin/<id>, #consultations[/<id>], #documents[/<id>], #documents/c/<id> ->
// route word (+ "/c") (+ "/[id]"); other hashes dropped.
function safeHash(hash) {
  const m = /^#(admin|consultations|documents)(\/c(?=\/))?(\/[^?]*)?(?:\?.*)?$/s.exec(hash || '')
  if (!m) return ''
  return `#${m[1]}` + (m[1] === 'documents' && m[2] ? '/c' : '')
    + (m[3] && m[3].length > 1 ? `/${ID}` : '')
}

// A page URL (location, navigation from/to, pageload/transaction name).
export function pageUrl(value) {
  const u = parseUrl(value)
  if (!u) return undefined
  const segs = u.path.split('/').slice(1).map((s) => (s === '' || APP_PATH_WORDS.has(s) ? s : ID))
  return `${u.origin}/${segs.join('/')}${safeHash(u.hash)}`
}

// A request URL (fetch/xhr, http.client, resources): our API -> endpoint
// template; a bundle file -> its path; anything else -> origin only.
export function requestUrl(value) {
  const u = parseUrl(value)
  if (!u) return undefined
  const tpl = apiTemplate(u.path)
  if (tpl) return u.origin + tpl
  if (CODE_PATH.test(u.path)) return u.origin + u.path
  return u.origin || undefined
}

// A stack frame / source-map file: a bundle file keeps its path, else the page rule.
export function codeUrl(value) {
  const u = parseUrl(value)
  if (!u) return undefined
  return CODE_PATH.test(u.path) ? u.origin + u.path : pageUrl(value)
}

const method = (v) => (typeof v === 'string' && HTTP_METHODS.has(v.toUpperCase()) ? v.toUpperCase() : undefined)

// Span / breadcrumb data: EXACT key -> rule (F-39, no prefix matches). Keys
// without a rule are dropped. Numbers only for counts/durations/status codes.
const TIMING_KEYS = ['redirect_start', 'redirect_end', 'worker_start', 'fetch_start', 'domain_lookup_start',
  'domain_lookup_end', 'connect_start', 'secure_connection_start', 'connection_end', 'request_start',
  'response_start', 'response_end', 'time_to_first_byte'].map((k) => `http.request.${k}`)
const DATA_RULES = {
  url: requestUrl, 'http.url': requestUrl, 'url.full': requestUrl, 'lcp.url': requestUrl,
  from: pageUrl, to: pageUrl,
  method, 'http.method': method, 'http.request.method': method,
  status_code: statusCode, 'http.response.status_code': statusCode,
  'http.response_content_length': bytes, 'http.decoded_response_content_length': bytes,
  'http.response_transfer_size': bytes,
  ...Object.fromEntries(TIMING_KEYS.map((k) => [k, duration])),
  'http.request.same_origin': bool,
  'server.address': shape(HOST),
  'url.scheme': oneOf(new Set(['http', 'https'])),
  'url.same_origin': bool,
  'sentry.origin': shape(ORIGIN), 'sentry.op': shape(OP), 'sentry.source': oneOf(SOURCES),
  'sentry.idle_span_finish_reason': oneOf(new Set(['idleTimeout', 'finalTimeout', 'externalFinish', 'cancelled', 'heartbeatFailed'])),
  'resource.render_blocking_status': oneOf(new Set(['blocking', 'non-blocking'])),
  'network.protocol.name': oneOf(new Set(['http', 'h2', 'h3', 'quic'])),
  'network.protocol.version': shape(/^\d(?:\.\d)?$/),
}
const PAGE_OPS = new Set(['pageload', 'navigation'])

// `op`: pageload/navigation data URLs are page URLs (the location), not requests.
function safeData(data, op) {
  if (!data || typeof data !== 'object') return undefined
  const out = {}
  for (const [k, v] of Object.entries(data)) {
    if (!Object.prototype.hasOwnProperty.call(DATA_RULES, k)) continue
    const isUrl = k === 'url' || k === 'http.url' || k === 'url.full'
    const clean = isUrl && PAGE_OPS.has(op) ? pageUrl(v) : DATA_RULES[k](v)
    if (clean !== undefined) out[k] = clean
  }
  return Object.keys(out).length ? out : undefined
}

function pick(src, rules) {
  const out = {}
  if (!src || typeof src !== 'object') return out
  for (const [k, rule] of Object.entries(rules)) {
    const v = rule(src[k])
    if (v !== undefined) out[k] = v
  }
  return out
}

// ---- exceptions ----------------------------------------------------------------

const SAFE_VALUES = new Set([
  'Your session expired. Please sign in again.',
  "Couldn't load customers: the server sent a customer without a usable id.",
  "Couldn't load customers: the server sent an unexpected response.",
  "Couldn't load consultations: the server sent an unexpected response.",
  "Couldn't load the consultation: the server sent an unexpected response.",
  "Couldn't get the audio link.",
  "Couldn't load documents: the server sent an unexpected response.",
  "Couldn't load the document: the server sent an unexpected response.",
  "Couldn't load vendor document customers: the server sent an unexpected response.",
  "Couldn't load the customer's documents: the server sent an unexpected response.",
  "Couldn't prepare the upload: the server sent an unexpected response.",
  'Choose a PDF file.',
  'That file is not a PDF.',
  'That file is empty.',
  'That PDF is larger than 20 MB.',
  'Pick a customer first.',
  'Network error uploading to R2',
  'Timed out uploading to R2',
  'Failed to fetch',
  'Load failed',
  'NetworkError when attempting to fetch resource.',
])
// Known browser messages: kept as a fixed template, the variable part replaced (F-35).
const TEMPLATES = [
  [/^Cannot read properties of (undefined|null) \(reading .*\)$/s, (m) => `Cannot read properties of ${m[1]} (reading '${REDACTED}')`],
  [/^Cannot set properties of (undefined|null) \(setting .*\)$/s, (m) => `Cannot set properties of ${m[1]} (setting '${REDACTED}')`],
  [/^(undefined|null) is not an object \(evaluating .*\)$/s, (m) => `${m[1]} is not an object (evaluating '${REDACTED}')`],
  [/^can't access property .*, .* is (undefined|null)$/s, (m) => `can't access property '${REDACTED}', ${REDACTED} is ${m[1]}`],
  [/^.+ is not a function$/s, () => `${REDACTED} is not a function`],
  [/^.+ is not defined$/s, () => `${REDACTED} is not defined`],
  [/^.+ is (undefined|null)$/s, (m) => `${REDACTED} is ${m[1]}`],
]
// Known-safe leading text; whatever follows becomes [redacted].
const SAFE_PREFIXES = [
  // api.js assertOk(opName): fixed op names + status; the server's `error` text is dropped.
  /^(?:Couldn't load customers|upload-url|submit|job-status|Couldn't load consultations|Couldn't load the consultation|Send to FieldPulse|Couldn't get the audio link|Delete|Couldn't load documents|Couldn't load the document|Upload document|Submit document|Delete document|Couldn't load vendor document customers|Couldn't load the customer's documents|Send vendor list) failed \(HTTP \d{3}\)/,
  /^R2 upload failed \(HTTP \d{1,3}\)/,
  /^Minified React error #\d+/,
  /^Objects are not valid as a React child/,
  /^Missing VITE_CLERK_PUBLISHABLE_KEY/,
]
const ERROR_TYPES = new Set([
  'Error', 'TypeError', 'ReferenceError', 'SyntaxError', 'RangeError', 'URIError', 'EvalError',
  'AggregateError', 'DOMException', 'AbortError', 'TimeoutError', 'NetworkError', 'NotAllowedError',
  'NotFoundError', 'NotReadableError', 'NotSupportedError', 'InvalidStateError', 'SecurityError',
  'QuotaExceededError', 'OverconstrainedError', 'UnknownError', 'ChunkLoadError', 'UnhandledRejection',
  'ClerkAPIResponseError', 'ClerkRuntimeError',
])
const BOUNDARY = 'React ErrorBoundary '

export function safeExceptionValue(value) {
  const text = typeof value === 'string' ? value.trim() : ''
  if (SAFE_VALUES.has(text)) return text
  for (const [re, build] of TEMPLATES) {
    const m = re.exec(text)
    if (m) return build(m)
  }
  for (const p of SAFE_PREFIXES) {
    const m = p.exec(text)
    if (m) return m[0] + (text.length > m[0].length ? ': ' + REDACTED : '')
  }
  return REDACTED
}

export function safeExceptionType(type) {
  if (typeof type !== 'string') return 'Error'
  if (type.startsWith(BOUNDARY)) return BOUNDARY + safeExceptionType(type.slice(BOUNDARY.length))
  return ERROR_TYPES.has(type) ? type : 'Error'
}

const CODE_NAME = /^[\w$.<>[\] /-]{1,200}$/ // function names: identifiers from the bundle
function frame(f) {
  if (!f || typeof f !== 'object') return undefined
  const out = pick(f, {
    function: shape(CODE_NAME), lineno: num, colno: num, in_app: bool,
    filename: (v) => codeUrl(v) ?? shape(/^<?[\w ]{1,32}>?$/)(v),
    abs_path: (v) => codeUrl(v) ?? shape(/^<?[\w ]{1,32}>?$/)(v),
  })
  return out
}
function stacktrace(st) {
  const frames = Array.isArray(st?.frames) ? st.frames.map(frame).filter(Boolean) : []
  return frames.length ? { frames } : undefined
}

function exception(ex) {
  if (!ex || typeof ex !== 'object') return undefined
  const out = { type: safeExceptionType(ex.type), value: safeExceptionValue(ex.value) }
  const mech = pick(ex.mechanism, {
    type: shape(/^[a-z][a-z_.]{0,63}$/), handled: bool, synthetic: bool,
    exception_id: num, parent_id: num, is_exception_group: bool,
    source: shape(/^(?:cause|errors\[\d{1,3}\])$/),
  })
  if (Object.keys(mech).length) out.mechanism = mech
  const st = stacktrace(ex.stacktrace)
  if (st) out.stacktrace = st
  return out
}

// ---- breadcrumbs ---------------------------------------------------------------

const CRUMB_CATEGORIES = new Set(['fetch', 'xhr', 'navigation', 'ui.click', 'ui.input', 'ui.keypress', 'sentry.event', 'sentry.transaction'])
const KEEP_CRUMB_DATA = new Set(['fetch', 'xhr', 'navigation'])

// beforeBreadcrumb, and again per breadcrumb at send time. null = drop.
export function scrubBreadcrumb(crumb) {
  if (!crumb || typeof crumb !== 'object') return null
  if (!CRUMB_CATEGORIES.has(crumb.category)) return null // console, custom, unknown
  const out = pick(crumb, {
    timestamp: num, level: oneOf(LEVELS), category: (v) => v,
    type: oneOf(new Set(['default', 'http', 'navigation', 'ui', 'user', 'error', 'info', 'debug'])),
  })
  if (KEEP_CRUMB_DATA.has(crumb.category)) {
    const data = safeData(crumb.data, crumb.category)
    if (data) out.data = data
  }
  return out // message never kept (element text, event text, custom text)
}

// ---- spans / transactions --------------------------------------------------------

function description(op, desc) {
  if (typeof desc !== 'string' || !op) return undefined
  if (op.startsWith('http')) {
    const m = /^([A-Za-z]+) (\S+)$/.exec(desc.trim())
    const url = m && method(m[1]) && requestUrl(m[2])
    return url ? `${m[1].toUpperCase()} ${url}` : undefined
  }
  if (op.startsWith('resource.')) return requestUrl(desc)
  if (PAGE_OPS.has(op)) return pageUrl(desc)
  return undefined
}

function traceContext(t) {
  if (!t || typeof t !== 'object') return undefined
  const out = pick(t, {
    trace_id: shape(HEX32), span_id: shape(HEX16), parent_span_id: shape(HEX16),
    op: (v) => shape(OP)(v) ?? (v === undefined ? undefined : 'default'),
    status: oneOf(SPAN_STATUS), origin: shape(ORIGIN),
  })
  const data = safeData(t.data, out.op)
  if (data) out.data = data
  return out
}

function span(s) {
  if (!s || typeof s !== 'object') return undefined
  const out = pick(s, {
    span_id: shape(HEX16), parent_span_id: shape(HEX16), trace_id: shape(HEX32),
    start_timestamp: num, timestamp: num, exclusive_time: num,
    status: oneOf(SPAN_STATUS), origin: shape(ORIGIN),
  })
  out.op = shape(OP)(s.op) ?? 'default'
  const desc = description(out.op, s.description)
  if (desc) out.description = desc
  const data = safeData(s.data, out.op)
  if (data) out.data = data
  return out
}

const MEASUREMENTS = new Set(['fp', 'fcp', 'lcp', 'cls', 'ttfb', 'ttfb.requestTime', 'inp', 'fid', 'connection.rtt'])
function measurements(m) {
  if (!m || typeof m !== 'object') return undefined
  const out = {}
  for (const [k, v] of Object.entries(m)) {
    if (MEASUREMENTS.has(k) && duration(v?.value) !== undefined) { // ms / score, bounded (F-39)
      out[k] = { value: v.value }
      if (typeof v.unit === 'string' && /^[a-z]{0,12}$/.test(v.unit)) out[k].unit = v.unit
    }
  }
  return Object.keys(out).length ? out : undefined
}

// Transaction / page name: a page path only (pageload uses the URL path).
const transactionName = (v) => (typeof v === 'string' ? pageUrl(v) ?? REDACTED : undefined)

// ---- sdk metadata (F-41) ---------------------------------------------------------

export const SDK_NAME = 'sentry.javascript.react'
const JS_INTEGRATIONS = new Set(['EventFilters', 'InboundFilters', 'FunctionToString', 'ConversationId',
  'BrowserApiErrors', 'Breadcrumbs', 'GlobalHandlers', 'LinkedErrors', 'Dedupe', 'HttpContext',
  'CultureContext', 'BrowserSession', 'BrowserTracing', 'WebVitals'])
const SDK_VERSION_SHAPE = /^\d{1,3}\.\d{1,3}\.\d{1,3}(?:-[\w.]{1,32})?$/

// From our constants (own.sdkVersion = the installed SDK's SDK_VERSION), never
// the payload; integration names only from the fixed list.
function trustedSdk(sdk, own, withIntegrations = true) {
  const version = shape(SDK_VERSION_SHAPE)(own?.sdkVersion)
  if (!version) return undefined
  const out = { name: SDK_NAME, version }
  if (withIntegrations && Array.isArray(sdk?.integrations)) {
    const names = [...new Set(sdk.integrations.filter((n) => typeof n === 'string' && JS_INTEGRATIONS.has(n)))]
    if (names.length) out.integrations = names
  }
  return out
}

// ---- events --------------------------------------------------------------------

// beforeSend / beforeSendTransaction: a NEW event with only allowlisted fields.
// own = { sdkVersion, ... } (our constants, see lib/sentry.jsx).
export function scrubEvent(event, own = {}) {
  const out = pick(event, {
    event_id: shape(HEX32), timestamp: num, start_timestamp: num, platform: oneOf(new Set(['javascript'])),
    level: oneOf(LEVELS), type: oneOf(new Set(['transaction'])),
    environment: shape(/^[\w.-]{1,64}$/), release: shape(/^[\w.@+-]{1,128}$/), dist: shape(/^[\w.-]{1,64}$/),
    transaction: transactionName,
  })
  // sdk: rebuilt from our constants (F-41). sdkProcessingMetadata: read by the
  // SDK for the envelope trace header (which the transport rebuilds), deleted by
  // the SDK before sending, and deleted again at the transport.
  const sdk = trustedSdk(event.sdk, own)
  if (sdk) out.sdk = sdk
  if (event.sdkProcessingMetadata) out.sdkProcessingMetadata = event.sdkProcessingMetadata
  const source = oneOf(SOURCES)(event.transaction_info?.source)
  if (source) out.transaction_info = { source }
  if (event.request) {
    const url = pageUrl(event.request.url)
    out.request = url ? { url } : {}
  }
  const values = (event.exception?.values || []).map(exception).filter(Boolean)
  if (values.length) out.exception = { values }
  if (event.message !== undefined || event.logentry !== undefined) out.message = '[redacted message]'
  if (Array.isArray(event.breadcrumbs)) out.breadcrumbs = event.breadcrumbs.map(scrubBreadcrumb).filter(Boolean)
  const trace = traceContext(event.contexts?.trace)
  if (trace) out.contexts = { trace }
  if (Array.isArray(event.spans)) out.spans = event.spans.map(span).filter(Boolean)
  const m = measurements(event.measurements)
  if (m) out.measurements = m
  const images = (event.debug_meta?.images || [])
    .filter((i) => i?.type === 'sourcemap' && UUID.test(i.debug_id || '') && codeUrl(i.code_file))
    .map((i) => ({ type: 'sourcemap', debug_id: i.debug_id, code_file: codeUrl(i.code_file) }))
  if (images.length) out.debug_meta = { images }
  return out
}

// ---- envelope (final boundary, in the transport) -----------------------------------

const ITEM_TYPES = new Set(['event', 'transaction', 'session', 'sessions', 'client_report'])

// Dynamic sampling context: only these keys, each validated; public_key,
// environment and release are always our own (from `own`).
export function safeTraceHeader(trace, own = {}) {
  if (!trace || typeof trace !== 'object') return undefined
  const out = pick(trace, {
    trace_id: shape(HEX32),
    sample_rate: shape(/^(?:0|1|0?\.\d{1,16}|1\.0+|\d(?:\.\d+)?e-\d{1,3})$/),
    sample_rand: shape(/^(?:0|0?\.\d{1,16})$/),
    sampled: oneOf(new Set(['true', 'false'])),
  })
  if (!out.trace_id) return undefined
  for (const k of ['public_key', 'environment', 'release']) if (typeof own[k] === 'string' && own[k]) out[k] = own[k]
  return out
}

// ---- sessions (F-40): rebuilt from a strict schema; attrs always ours ----------------

const SESSION_STATUS = new Set(['ok', 'exited', 'crashed', 'abnormal'])
const ABNORMAL = new Set(['anr_foreground', 'anr_background'])

function sessionAttrs(own) {
  if (typeof own.release !== 'string' || !own.release) return undefined // Sentry ignores these
  return { release: own.release, ...(typeof own.environment === 'string' && own.environment ? { environment: own.environment } : {}) }
}

function safeSession(p, own) {
  const attrs = sessionAttrs(own)
  if (!attrs || !p || typeof p !== 'object') return undefined
  const out = pick(p, {
    sid: shape(UUID), init: bool, started: shape(ISO), timestamp: shape(ISO), status: oneOf(SESSION_STATUS),
    errors: count, duration, seq: count, abnormal_mechanism: oneOf(ABNORMAL),
  })
  if (!out.sid || !out.started) return undefined
  out.attrs = attrs
  return out
}

function safeAggregates(p, own) {
  const attrs = sessionAttrs(own)
  const rows = Array.isArray(p?.aggregates) ? p.aggregates : []
  const aggregates = rows.map((a) => pick(a, { started: shape(ISO), exited: count, errored: count, crashed: count, abnormal: count }))
    .filter((a) => a.started)
  return attrs && aggregates.length ? { attrs, aggregates } : undefined
}

function safeItem([h, payload], own) {
  const type = h.type
  let clean = payload
  if (type === 'session') clean = safeSession(payload, own)
  else if (type === 'sessions') clean = safeAggregates(payload, own)
  else if (type === 'event' || type === 'transaction') {
    if (!payload || typeof payload !== 'object') return undefined
    clean = { ...payload }
    delete clean.sdkProcessingMetadata
    const sdk = trustedSdk(payload.sdk, own)
    if (sdk) clean.sdk = sdk
    else delete clean.sdk
  }
  return clean === undefined ? undefined : [{ type }, clean]
}

// envelope = [headers, [[itemHeaders, payload], ...]] (unserialised). Returns
// a cleaned envelope, or null when nothing is left to send.
export function safeEnvelope(envelope, own = {}) {
  if (!Array.isArray(envelope) || !Array.isArray(envelope[1])) return null
  const [headers, items] = envelope
  const kept = items.filter((it) => Array.isArray(it) && ITEM_TYPES.has(it[0]?.type))
    .map((it) => safeItem(it, own)).filter(Boolean)
  if (!kept.length) return null
  const outHeaders = pick(headers, { event_id: shape(HEX32), sent_at: shape(ISO) })
  if (headers?.sdk) {
    const sdk = trustedSdk(undefined, own, false)
    if (sdk) outHeaders.sdk = sdk
  }
  const trace = safeTraceHeader(headers?.trace, own)
  if (trace) outHeaders.trace = trace
  return [outHeaders, kept]
}
