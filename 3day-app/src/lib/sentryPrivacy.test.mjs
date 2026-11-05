// Privacy regressions for lib/sentryPrivacy.js (F-31, F-32). No dependencies:
//   node --test src/lib/sentryPrivacy.test.mjs        (from 3day-app/)
// Plants customer data, credentials and signed URL queries in every place the
// browser SDK puts text, then asserts none of it is in the serialized event.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  codeUrl, pageUrl, requestUrl, safeEnvelope, safeExceptionType, safeExceptionValue, safeTraceHeader,
  scrubBreadcrumb, scrubEvent, SDK_NAME,
} from './sentryPrivacy.js'

const NAME = 'FrontendPlantedName'
const PHONE = '+15555550124'
const ADDRESS = '998 Synthetic Lane'
const TRANSCRIPT = 'FRONTEND_PRIVATE_TRANSCRIPT'
const SECRETS = ['FRONTEND_AUTH_SECRET', 'FRONTEND_COOKIE_SECRET', 'FRONTEND_INTAKE_SECRET', 'SIGNED_SECRET', 'ADMINQUERY']
const PLANTED = [NAME, PHONE, ADDRESS, TRANSCRIPT, ...SECRETS]
const SIGNED = 'https://acct.r2.cloudflarestorage.com/b/consultations/a.webm?X-Amz-Signature=SIGNED_SECRET'
const CUSTOMER = { name: NAME, phone: PHONE, address: ADDRESS, transcript: TRANSCRIPT, url: SIGNED }

function assertClean(obj) {
  const blob = JSON.stringify(obj)
  for (const s of PLANTED) assert.ok(!blob.includes(s), `${s} leaked: ${blob}`)
}

function crumbs(list) {
  return list.map(scrubBreadcrumb).filter(Boolean)
}

test('console breadcrumbs are dropped (F-32 repro)', () => {
  const out = crumbs([
    { category: 'console', level: 'warning', message: `Synthetic customer record ${NAME}`,
      data: { arguments: ['Synthetic customer record', CUSTOMER], logger: 'console' } },
    { category: 'console', level: 'log', message: 'x',
      data: { arguments: [{ Authorization: 'Bearer FRONTEND_AUTH_SECRET', Cookie: 'FRONTEND_COOKIE_SECRET',
        'x-intake-key': 'FRONTEND_INTAKE_SECRET' }] } },
  ])
  assert.deepEqual(out, [])
})

test('fetch/xhr/navigation keep method, status and query-less URLs; others lose text', () => {
  const out = crumbs([
    { type: 'http', category: 'fetch', level: 'info',
      data: { method: 'GET', url: `https://api.example/admin/consultations?q=ADMINQUERY`, status_code: 200,
        request_body_size: 10, 'http.query': '?q=ADMINQUERY', body: TRANSCRIPT } },
    { type: 'http', category: 'xhr', data: { method: 'PUT', url: SIGNED, status_code: 403 } },
    { category: 'navigation', data: { from: '/?q=ADMINQUERY#admin', to: `/#admin/job_1a2b3c4d5e6f?x=${NAME}` } },
    { category: 'ui.click', message: `button[title="${NAME}"]` },
    { category: 'sentry.event', message: `Error: ${NAME} ${PHONE}` },
    { category: 'custom', message: TRANSCRIPT, data: { customer: CUSTOMER } },
  ])
  assertClean(out)
  assert.deepEqual(out[0].data, { method: 'GET', url: 'https://api.example/admin/consultations',
    status_code: 200 })
  assert.equal(out[1].data.url, 'https://acct.r2.cloudflarestorage.com') // not our API: origin only (F-38)
  assert.deepEqual(out[2].data, { from: '/#admin', to: '/#admin/[id]' })
  assert.equal(out[3].category, 'ui.click')
  assert.equal(out[3].message, undefined)
  assert.equal(out.length, 5) // the custom crumb is dropped whole
})

test('exception values: API error bodies, React object keys and free text are redacted', () => {
  assert.equal(safeExceptionValue(`submit failed (HTTP 500): ${NAME} ${PHONE}`),
    'submit failed (HTTP 500): [redacted]')
  assert.equal(safeExceptionValue('job-status failed (HTTP 404)'), 'job-status failed (HTTP 404)')
  assert.equal(safeExceptionValue(`R2 upload failed (HTTP 403): <Error>${SIGNED}</Error>`),
    'R2 upload failed (HTTP 403): [redacted]')
  assert.equal(safeExceptionValue(`Objects are not valid as a React child (found: object with keys {${NAME}})`),
    'Objects are not valid as a React child: [redacted]')
  assert.equal(safeExceptionValue(`Minified React error #31; visit https://react.dev/errors/31?args[]=${NAME}`),
    'Minified React error #31: [redacted]')
  assert.equal(safeExceptionValue(`${NAME} ${ADDRESS} ${TRANSCRIPT}`), '[redacted]')
  assert.equal(safeExceptionValue(`${NAME} failed (HTTP 500)`), '[redacted]')
  assert.equal(safeExceptionValue('Your session expired. Please sign in again.'),
    'Your session expired. Please sign in again.')
  assert.equal(safeExceptionValue("Cannot read properties of undefined (reading 'scope')"),
    "Cannot read properties of undefined (reading '[redacted]')")
})

test('full event: nothing planted survives; type, frames and hash route kept', () => {
  const event = {
    exception: { values: [
      { type: 'Error', value: `boom ${NAME} ${TRANSCRIPT}`, mechanism: { type: 'onerror', data: { x: NAME } },
        stacktrace: { frames: [{ function: 'AdminDetail', filename: `https://app.example/assets/index.js?t=${NAME}`,
          lineno: 10, vars: { customer: CUSTOMER } }] } },
      { type: 'TypeError', value: `inner ${PHONE}` },
    ] },
    message: `captured ${NAME}`,
    request: { url: `https://app.example/?q=ADMINQUERY#admin/job_1a2b3c4d5e6f`,
      headers: { Authorization: 'Bearer FRONTEND_AUTH_SECRET', Cookie: 'FRONTEND_COOKIE_SECRET' } },
    extra: { customer: CUSTOMER },
    user: { email: 'zelda@example.invalid', username: NAME },
    contexts: { react: { componentStack: '\n    at AdminDetail' }, state: { customer: CUSTOMER },
      trace: { trace_id: 't', span_id: 's', data: { 'http.query': 'q=ADMINQUERY', 'sentry.op': 'pageload' } } },
    breadcrumbs: [
      { category: 'console', data: { arguments: [CUSTOMER] } },
      { category: 'fetch', data: { method: 'POST', url: `https://api.example/consultation/submit?k=FRONTEND_INTAKE_SECRET` } },
    ],
    transaction: '/?q=ADMINQUERY',
  }
  const out = scrubEvent(event)
  assertClean(out)
  assert.equal(out.exception.values[0].type, 'Error')
  assert.equal(out.exception.values[0].value, '[redacted]')
  assert.equal(out.exception.values[0].stacktrace.frames[0].function, 'AdminDetail')
  assert.equal(out.exception.values[0].stacktrace.frames[0].lineno, 10)
  assert.equal(out.request.url, 'https://app.example/#admin/[id]')
  assert.equal(out.exception.values[0].stacktrace.frames[0].filename, 'https://app.example/assets/index.js')
  assert.equal(out.message, '[redacted message]')
  assert.equal(out.extra, undefined)
  assert.equal(out.user, undefined)
  assert.deepEqual(Object.keys(out.contexts), ['trace'])
  assert.deepEqual(out.contexts.trace, { data: { 'sentry.op': 'pageload' } }) // bad ids dropped
  assert.equal(out.breadcrumbs.length, 1)
  assert.equal(out.transaction, '/')
})

test('transaction spans: query-less descriptions, allowlisted data', () => {
  const out = scrubEvent({
    type: 'transaction',
    spans: [
      { op: 'resource.other', description: SIGNED, data: { 'http.response_content_length': 99, 'url.query': 'X-Amz-Signature=SIGNED_SECRET' } },
      { op: 'http.client', description: `GET https://api.example/admin/consultations?q=ADMINQUERY`,
        data: { 'http.method': 'GET', url: 'https://api.example/admin/consultations?q=ADMINQUERY',
          'http.query': '?q=ADMINQUERY', 'http.request.header.authorization': 'Bearer FRONTEND_AUTH_SECRET',
          'http.response.status_code': 200 } },
    ],
  })
  assertClean(out)
  assert.equal(out.spans[0].description, 'https://acct.r2.cloudflarestorage.com')
  assert.deepEqual(out.spans[1].data, { 'http.method': 'GET', url: 'https://api.example/admin/consultations',
    'http.response.status_code': 200 })
})

test('pageUrl keeps the hash route word only; requestUrl maps our API to templates', () => {
  assert.equal(pageUrl('https://a.example/?y=1#admin/job_1'), 'https://a.example/#admin/[id]')
  assert.equal(pageUrl('https://a.example/x#admin?y=1'), 'https://a.example/[id]#admin')
  assert.equal(pageUrl('https://a.example/index.html#consultations'), 'https://a.example/index.html#consultations')
  assert.equal(pageUrl(`https://a.example/#%7B%22name%22%3A%22${NAME}%22%7D`), 'https://a.example/')
  assert.equal(pageUrl(`https://a.example/#admin/${NAME} ${PHONE}`), 'https://a.example/#admin/[id]')
  assert.equal(requestUrl('/app/customers?q=1'), '/app/customers')
  assert.equal(requestUrl('https://api.example/job-status/job_1a2b?x=1'), 'https://api.example/job-status/[id]')
  assert.equal(requestUrl('https://api.example/admin/consultations/job_1/audio-url'), 'https://api.example/admin/consultations/[id]/audio-url')
  assert.equal(requestUrl(`https://api.example/admin/consultations/${NAME}/x`), 'https://api.example')
  assert.equal(requestUrl('https://app.example/assets/index-AbC123_x.js'), 'https://app.example/assets/index-AbC123_x.js')
  assert.equal(codeUrl(`https://app.example/${NAME}`), 'https://app.example/[id]')
  assert.equal(pageUrl(`not a url ${NAME}`), undefined)
  assert.equal(requestUrl(`not a url ${NAME}`), undefined)
})

// ---- round 3 (F-33..F-37) ----

const P = 'R3BrowserCustomer'
function assertNoP(obj) {
  const blob = JSON.stringify(obj)
  assert.ok(!blob.includes(P), `${P} leaked: ${blob}`)
}

test('F-36: URL userinfo (username and password) is removed everywhere', () => {
  const cred = `https://user:${P}@example.invalid/audio?sig=secret`
  assert.equal(requestUrl(cred), 'https://example.invalid')
  assert.equal(pageUrl(`https://${P}@example.invalid/a`), 'https://example.invalid/[id]')
  const crumb = scrubBreadcrumb({ category: 'fetch', type: 'http', data: { url: cred, method: 'GET' } })
  assert.deepEqual(crumb.data, { url: 'https://example.invalid', method: 'GET' })
  const out = scrubEvent({
    request: { url: cred },
    exception: { values: [{ type: 'Error', value: 'x', stacktrace: { frames: [{ filename: cred, abs_path: cred }] } }] },
    spans: [{ op: 'http.client', description: `GET ${cred}`, data: { url: cred } },
      { op: 'resource.script', description: cred }],
  })
  assertNoP(out)
  assert.equal(out.spans[0].description, 'GET https://example.invalid')
  assert.equal(out.spans[1].description, 'https://example.invalid')
})

test('F-35: browser TypeErrors keep a fixed template, not the property/expression', () => {
  let message = ''
  try { const absent = null; absent[P] } catch (e) { message = e.message } // real V8 text
  assert.ok(message.includes(P))
  assert.equal(safeExceptionValue(message), "Cannot read properties of null (reading '[redacted]')")
  for (const m of [`undefined is not an object (evaluating 'a.${P}')`, `${P} is not a function`,
    `${P} is not defined`, `can't access property "${P}", x is undefined`, `${P} is null`,
    `Cannot set properties of undefined (setting '${P}')`]) {
    const v = safeExceptionValue(m)
    assert.ok(!v.includes(P) && v.includes('[redacted]'), v)
  }
  // Exception type: known names only.
  assert.equal(safeExceptionType('TypeError'), 'TypeError')
  assert.equal(safeExceptionType(P), 'Error')
  assert.equal(safeExceptionType(`React ErrorBoundary ${P}`), 'React ErrorBoundary Error')
  assert.equal(safeExceptionType('React ErrorBoundary TypeError'), 'React ErrorBoundary TypeError')
})

test('F-37: allowed containers with unsafe contents lose the contents', () => {
  const out = scrubEvent({
    contexts: { react: { componentStack: P }, os: { name: P }, culture: { timezone: P },
      trace: { trace_id: 'a'.repeat(32), span_id: 'b'.repeat(16), op: P, status: P, origin: P,
        data: { 'sentry.op': P, 'sentry.origin': P, 'sentry.source': P, 'server.address': `${P} x`,
          'browser.web_vital': P, 'http.method': P, 'url.scheme': P } } },
    tags: { who: P }, fingerprint: [P], extra: { x: P }, user: { id: P }, modules: { [P]: '1' },
    breadcrumbs: [{ category: 'fetch', type: P, level: P, message: P, data: { method: P, status_code: P, url: P } },
      { category: P, message: P }],
    exception: { values: [{ type: P, value: P, module: P,
      mechanism: { type: P, handled: true, data: { x: P }, source: P } }] },
    transaction: P, environment: `${P} x`, server_name: P, logentry: { message: P },
  })
  assertNoP(out)
  assert.deepEqual(out.contexts.trace, { trace_id: 'a'.repeat(32), span_id: 'b'.repeat(16), op: 'default' })
  assert.equal(out.transaction, '[redacted]')
  assert.deepEqual(out.exception.values[0], { type: 'Error', value: '[redacted]', mechanism: { handled: true } })
})

test('F-33: transaction spans (static lifecycle) keep only validated op/description/data', () => {
  const out = scrubEvent({
    type: 'transaction', transaction: P, transaction_info: { source: 'custom' },
    contexts: { trace: { trace_id: 'a'.repeat(32), span_id: 'b'.repeat(16), op: 'test',
      data: { 'customer.transcript': P, 'sentry.op': 'test' } } },
    spans: [{ span_id: 'c'.repeat(16), op: 'test', description: P,
      data: { 'customer.transcript': P, 'http.request.body': P,
        'url.full': `https://example.invalid/audio?X-Amz-Signature=${P}`,
        'http.request.header.authorization': `Bearer ${P}`, 'customer.phone': 5555550199 } },
    { op: 'http.client', description: `GET https://api.example/x?q=${P}`, status: 'ok', origin: 'auto.http.browser' },
    { op: 'mark', description: P }],
    measurements: { lcp: { value: 1.5, unit: 'millisecond' }, [P]: { value: 1 } },
  })
  assertNoP(out)
  assert.equal(out.transaction, '[redacted]')
  assert.equal(out.contexts.trace.op, 'default')
  assert.deepEqual(out.spans[0], { span_id: 'c'.repeat(16), op: 'default', data: { 'url.full': 'https://example.invalid' } })
  assert.deepEqual(out.spans[1], { op: 'http.client', description: 'GET https://api.example', status: 'ok', origin: 'auto.http.browser' })
  assert.equal(out.spans[2].description, undefined)
  assert.deepEqual(Object.keys(out.measurements), ['lcp'])
})

test('F-33/F-34: transport boundary drops non-scrubbed items and rebuilds the trace header', () => {
  const own = { public_key: 'ownkey', environment: 'production', release: 'abc123' }
  const env = [
    { event_id: 'd'.repeat(32), sent_at: '2026-11-04T00:00:00Z', sdk: { name: 'sentry.javascript.react' },
      trace: { trace_id: 'e'.repeat(32), public_key: P, transaction: P, user_segment: P, replay_id: P,
        sample_rate: '1', sampled: 'true', sample_rand: '0.42', environment: P, release: P, org_id: P } },
    [[{ type: 'span', item_count: 1 }, { items: [{ name: P, attributes: { x: { value: P } } }] }],
      [{ type: 'log' }, { items: [{ body: P }] }],
      [{ type: 'replay_recording' }, P],
      [{ type: 'attachment', filename: P }, P],
      [{ type: 'event' }, { event_id: 'd'.repeat(32) }],
      [{ type: 'session' }, { sid: 'f'.repeat(32), started: '2026-11-05T17:47:06.653Z', did: P,
        attrs: { release: 'abc123', environment: 'production', user_agent: P, ip_address: P } }]],
  ]
  const out = safeEnvelope(env, own)
  assertNoP(out)
  assert.deepEqual(out[0].trace, { trace_id: 'e'.repeat(32), sample_rate: '1', sample_rand: '0.42', sampled: 'true',
    public_key: 'ownkey', environment: 'production', release: 'abc123' })
  assert.deepEqual(out[1].map(([h]) => h.type), ['event', 'session'])
  assert.equal(safeEnvelope([{}, [[{ type: 'span' }, {}]]], own), null)
  assert.equal(safeTraceHeader({ trace_id: P }, own), undefined)
})

// ---- round 4 (F-38..F-41): whole envelope, as serialised for HTTP ----

const P4 = 'R4BrowserPrivate'
const PHONE4 = 15555550388
const OWN4 = { public_key: 'ownkey', environment: 'production', release: 'abc123', sdkVersion: '11.10.0' }
// The SDK's own envelope serialisation is JSON lines; this is the same bytes check.
function wire(envelope) {
  assert.ok(envelope, 'nothing would be sent')
  const [headers, items] = envelope
  return [JSON.stringify(headers), ...items.flatMap(([h, p]) => [JSON.stringify(h), typeof p === 'string' ? p : JSON.stringify(p)])].join('\n')
}
function assertNoP4(text) {
  for (const s of [P4, P4.toLowerCase(), String(PHONE4), '5555550388']) assert.ok(!text.includes(s), `${s} leaked: ${text}`)
}
// What the SDK does: beforeSend(Transaction) -> envelope -> our transport.
function send(event, type = 'event', headers = {}) {
  const scrubbed = scrubEvent(event, OWN4)
  return safeEnvelope([{ event_id: 'd'.repeat(32), sent_at: '2026-11-05T17:47:06.655Z', ...headers },
    [[{ type }, scrubbed]]], OWN4)
}

test('F-38: pushState path and #admin id never leave (page, crumbs, trace, spans, frames)', () => {
  const page = `http://127.0.0.1:5179/${P4}#admin/${P4}`
  const env = send({
    type: 'transaction', transaction: `/${P4}`, transaction_info: { source: 'url' },
    request: { url: page },
    contexts: { trace: { trace_id: 'a'.repeat(32), span_id: 'b'.repeat(16), op: 'navigation', data: { 'url.full': page } } },
    breadcrumbs: [{ category: 'navigation', data: { from: '/backtester-sentry.html', to: `/${P4}#admin/${P4}` } },
      { category: 'fetch', type: 'http', data: { method: 'GET', url: `http://localhost:5199/job-status/${P4}`, status_code: 200 } },
      { category: 'xhr', type: 'http', data: { method: 'PUT', url: `https://acct.r2.cloudflarestorage.com/b/${P4}.webm`, status_code: 200 } }],
    spans: [{ op: 'http.client', description: `GET http://localhost:5199/admin/consultations/${P4}`, data: { 'url.full': `http://localhost:5199/admin/consultations/${P4}?q=1` } },
      { op: 'resource.other', description: `https://cdn.example/${P4}.png` },
      { op: 'browser.request', data: { 'url.full': page } }],
    exception: { values: [{ type: 'Error', value: 'path probe', stacktrace: { frames: [{ filename: page, function: 'x' }] } }] },
  }, 'transaction')
  const text = wire(env)
  assertNoP4(text)
  const ev = env[1][0][1]
  assert.equal(ev.transaction, '/[id]')
  assert.equal(ev.request.url, 'http://127.0.0.1:5179/[id]#admin/[id]')
  assert.equal(ev.contexts.trace.data['url.full'], 'http://127.0.0.1:5179/[id]#admin/[id]')
  assert.deepEqual(ev.breadcrumbs.map((c) => c.data.to ?? c.data.url),
    ['/[id]#admin/[id]', 'http://localhost:5199/job-status/[id]', 'https://acct.r2.cloudflarestorage.com'])
  assert.equal(ev.spans[0].description, 'GET http://localhost:5199/admin/consultations/[id]')
  assert.equal(ev.spans[1].description, 'https://cdn.example')
})

test('F-39: only exact data keys; numbers only where they mean a count/duration/status', () => {
  const env = send({
    type: 'transaction', transaction: '/',
    contexts: { trace: { trace_id: 'a'.repeat(32), span_id: 'b'.repeat(16), op: 'pageload',
      data: { [`sentry.${P4}`]: 1, 'sentry.customer_phone': PHONE4, 'browser.x': PHONE4, 'http.request.x': PHONE4 } } },
    spans: [{ op: `http.${P4.toLowerCase()}`, data: { [`sentry.${P4}`]: 1, 'sentry.customer_phone': PHONE4,
      'http.response.status_code': PHONE4, 'http.response_content_length': PHONE4, 'http.request.fetch_start': PHONE4,
      [`resource.${P4}`]: true, 'http.request.response_end': 12.5 } }],
    measurements: { [P4]: { value: 1 }, lcp: { value: PHONE4 } },
  }, 'transaction')
  const text = wire(env)
  assertNoP4(text)
  const ev = env[1][0][1]
  assert.deepEqual(ev.spans[0].data, { 'http.request.response_end': 12.5 })
  assert.equal(ev.contexts.trace.data, undefined)
})

test('F-40: session and sessions items are rebuilt with our release/environment', () => {
  const env = safeEnvelope([{ sent_at: '2026-11-05T17:47:06.655Z', sdk: { name: P4 } }, [
    [{ type: 'session' }, { sid: '6e858793bc6049f2908e42a452e2eb9f', init: true, started: '2026-11-05T17:47:06.653Z',
      timestamp: '2026-11-05T17:47:06.653Z', status: 'ok', errors: 0, duration: 0, did: P4, abnormal_mechanism: P4,
      attrs: { release: P4, environment: P4, user_agent: P4 }, [P4]: P4 }],
    [{ type: 'session' }, { sid: P4, started: '2026-11-05T17:47:06.653Z', status: P4 }],
    [{ type: 'sessions' }, { attrs: { release: P4, environment: P4 },
      aggregates: [{ started: '2026-11-05T17:47:00.000Z', exited: 3, did: P4, errored: PHONE4, [P4]: 1 }] }],
  ]], OWN4)
  const text = wire(env)
  assertNoP4(text)
  assert.deepEqual(env[1].map(([h]) => h.type), ['session', 'sessions'])
  for (const [, p] of env[1]) assert.deepEqual(p.attrs, { release: 'abc123', environment: 'production' })
  assert.deepEqual(env[1][0][1], { sid: '6e858793bc6049f2908e42a452e2eb9f', init: true, started: '2026-11-05T17:47:06.653Z',
    timestamp: '2026-11-05T17:47:06.653Z', status: 'ok', errors: 0, duration: 0, attrs: { release: 'abc123', environment: 'production' } })
  assert.deepEqual(env[1][1][1].aggregates, [{ started: '2026-11-05T17:47:00.000Z', exited: 3 }])
  assert.equal(safeEnvelope([{}, [[{ type: 'session' }, { sid: 'f'.repeat(32), started: '2026-11-05T17:47:06.653Z' }]]], { ...OWN4, release: undefined }), null)
})

test('F-41: event and envelope-header sdk rebuilt from our constants', () => {
  const event = { message: 'sdk metadata probe', sdk: { name: P4, version: P4, customer: P4,
    integrations: ['BrowserTracing', P4], packages: [{ name: P4, version: P4 }] } }
  const env = send(event, 'event', { sdk: { name: P4, version: '11.10.0', customer: P4 } })
  const text = wire(env)
  assertNoP4(text)
  assert.deepEqual(env[0].sdk, { name: SDK_NAME, version: '11.10.0' })
  assert.deepEqual(env[1][0][1].sdk, { name: SDK_NAME, version: '11.10.0', integrations: ['BrowserTracing'] })
  // Transport boundary alone (an item that skipped beforeSend):
  const raw = safeEnvelope([{ sdk: { customer: P4 } }, [[{ type: 'event', [P4]: 1 }, { sdk: { customer: P4 }, sdkProcessingMetadata: { x: P4 } }]]], OWN4)
  assertNoP4(wire(raw))
  assert.deepEqual(raw[1][0], [{ type: 'event' }, { sdk: { name: SDK_NAME, version: '11.10.0' } }])
})
