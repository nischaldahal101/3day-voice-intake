// Ferguson carts display helpers (stage E2). Pure: no network, no React state.
// Every helper re-guards server JSON (any field can be missing or odd). Money comes
// from the server as decimal STRINGS ("1502.76"), never floats.

export const DASH = '—'
export const OTHER_ROOM = 'Other'

export const CART_STATUS_LABEL = {
  needs_customer: 'Needs customer',
  linked: 'Linked',
  sent: 'Sent',
}

export function cartStatusLabel(s) {
  return (typeof s === 'string' && CART_STATUS_LABEL[s]) || 'Unknown'
}

// List/detail badge (E3): "Linked: <name>" when linked to a named customer, else the label.
export function cartBadge(status, customerName) {
  const name = typeof customerName === 'string' ? customerName.trim() : ''
  if ((status === 'linked' || status === 'sent') && name) return `Linked: ${name}`
  return cartStatusLabel(status)
}

// The match line under the customer card (E3). The server's reason when there is one.
export function matchText(d) {
  if (!d || typeof d !== 'object') return null
  const reason = typeof d.match_reason === 'string' && d.match_reason.trim() ? d.match_reason.trim() : null
  if (d.match_state === 'pending') return reason || 'Looking for a matching FieldPulse customer…'
  if (reason) return reason
  return d.status === 'needs_customer' ? 'Not matched yet.' : null
}

// The detail reloads itself while the automatic match is running.
export function matchPending(d) {
  return !!d && typeof d === 'object' && d.status === 'needs_customer' && d.match_state === 'pending'
}

// #carts (list), #carts/<cart_id> (detail), else null.
export function parseCartsHash(hash) {
  const h = typeof hash === 'string' ? hash.replace(/^#\/?/, '') : ''
  if (/^carts\/?$/.test(h)) return { view: 'list' }
  const m = /^carts\/(cart_[0-9a-f]{12})$/.exec(h)
  if (m) return { view: 'detail', cartId: m[1] }
  return null
}

const AMOUNT = /^\d{1,12}(\.\d{1,4})?$/

// "1502.76" -> integer cents (1502.76 -> 150276), else null. Strings only.
export function toCents(v) {
  if (typeof v !== 'string' || !AMOUNT.test(v.trim())) return null
  const [whole, frac = ''] = v.trim().split('.')
  return Number(whole) * 100 + Math.round(Number((frac + '0000').slice(0, 4)) / 100)
}

function fmtCents(cents) {
  return (cents / 100).toLocaleString('en-US', {
    style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2,
  })
}

// "$1,502.76" or "—".
export function cartMoney(v) {
  const c = toCents(v)
  return c === null ? DASH : fmtCents(c)
}

function qtyOf(q) {
  return typeof q === 'number' && Number.isInteger(q) && q >= 1 ? q : null
}

// quantity x unit price, exact in cents; "—" when either is unreadable.
export function lineTotal(quantity, unitPrice) {
  const q = qtyOf(quantity)
  const c = toCents(unitPrice)
  return q === null || c === null ? DASH : fmtCents(q * c)
}

export function qtyText(q) {
  const n = qtyOf(q)
  return n === null ? DASH : String(n)
}

// "Brand Model" (either part may be missing).
export function itemTitle(it) {
  if (!it || typeof it !== 'object') return DASH
  const parts = [it.brand, it.model].filter((p) => typeof p === 'string' && p.trim())
  return parts.length ? parts.join(' ') : DASH
}

// The line's product type (E4 layout: FieldPulse title "<Type> - <Room>"). `typesInfo` is the
// cart's item_types marker: null = not decided yet (the worker decides right before the first
// send) -> { text: DASH }; decided but no type for this line -> flagged.
export const TYPE_UNKNOWN = "Type couldn't be identified"
export function itemTypeView(it, typesInfo) {
  if (!typesInfo || typeof typesInfo !== 'object') return { text: DASH, flag: false }
  const t = it && typeof it.item_type === 'string' ? it.item_type.trim() : ''
  return t ? { text: t, flag: false } : { text: TYPE_UNKNOWN, flag: true }
}

// Items grouped by room, rooms in the order they first appear; no room -> "Other".
export function groupByRoom(items) {
  const groups = []
  const byRoom = new Map()
  for (const it of Array.isArray(items) ? items : []) {
    if (!it || typeof it !== 'object' || Array.isArray(it)) continue
    const room = typeof it.room === 'string' && it.room.trim() ? it.room.trim() : OTHER_ROOM
    if (!byRoom.has(room)) {
      const g = { room, items: [] }
      byRoom.set(room, g)
      groups.push(g)
    }
    byRoom.get(room).items.push(it)
  }
  return groups
}

// Only plain https links are rendered as links (never javascript:, data:, ...).
export function safeLink(url) {
  if (typeof url !== 'string') return null
  try {
    const u = new URL(url)
    return u.protocol === 'https:' ? u.href : null
  } catch (_) {
    return null
  }
}

export function count(v) {
  return typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : null
}

// "2026-12-10" -> "12/10/2026" (dates only; anything else as given or "—").
export function expiryText(v) {
  if (typeof v !== 'string' || !v.trim()) return DASH
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v.trim())
  return m ? `${m[2]}/${m[3]}/${m[1]}` : v
}

export function deleteCartPrompt(projectName) {
  const p = typeof projectName === 'string' && projectName.trim() ? projectName : 'unknown'
  return 'Delete this Ferguson cart?\n\n' +
    `Project: ${p}\n\n` +
    'This removes the cart and the email it came from, from this app. It cannot be undone.'
}

// E4: the cart's FieldPulse state (server carts_panel.fp_state) -> short label.
export const FP_STATE_LABEL = {
  not_sent: 'Not sent',
  queued: 'Queued',
  sending: 'Sending…',
  dry_run: 'Dry run',
  held: 'Held (dry run)',
  sent: 'Sent',
  attention: 'Needs attention',
  refused: 'Needs fixing',
  superseded: 'Replaced by a newer cart',
}

export function fpStateLabel(key) {
  return (typeof key === 'string' && FP_STATE_LABEL[key]) || FP_STATE_LABEL.not_sent
}

// Button text: "Retry" after a problem, else "Send to FieldPulse".
export function sendButtonText(state) {
  return state === 'attention' ? 'Retry' : 'Send to FieldPulse'
}

// The detail keeps reloading while a send is queued or running.
export function fpBusy(fp) {
  return !!fp && typeof fp === 'object' && (fp.state === 'sending' || fp.state === 'queued')
}

export function sendCartPrompt(projectName, customerName) {
  const p = typeof projectName === 'string' && projectName.trim() ? projectName : 'unknown'
  const c = typeof customerName === 'string' && customerName.trim() ? customerName : 'the linked customer'
  return 'Send this cart to FieldPulse?\n\n' +
    `Project: ${p}\nCustomer: ${c}\n\n` +
    'It creates (or replaces) ONE draft estimate and ONE material list "Ferguson cart: ' + p +
    '". After it is sent, the customer can\'t be changed.'
}

// F-110: every link (automatic match or manual) queues an automatic send, but the server keeps
// no "queued" record for it. A linked cart with NO FieldPulse record yet = that send is still
// to come, so the detail waits for it (bounded, like a manual Send).
export function autoSendExpected(d) {
  if (!d || typeof d !== 'object' || d.status !== 'linked' || d.superseded_by) return false
  const fp = d.fieldpulse && typeof d.fieldpulse === 'object' ? d.fieldpulse : null
  if (!fp) return true
  return fp.raw_status == null && fp.updated_at == null && !fp.problem
}

// The detail waits on the worker (one bounded wait per FieldPulse record) when a send is
// queued/running or an automatic send is expected.
export function shouldWaitForSend(d) {
  if (!d || typeof d !== 'object') return false
  const fp = d.fieldpulse && typeof d.fieldpulse === 'object' ? d.fieldpulse : null
  return fpBusy(fp) || autoSendExpected(d)
}

export const SEND_WAIT_MAX_POLLS = 40 // ~2 min at 3 s

function fpOf(d) {
  return d && typeof d === 'object' && d.fieldpulse && typeof d.fieldpulse === 'object' ? d.fieldpulse : {}
}

// The record a wait watches (state + updated_at). F-112: ONE polling budget per record: the
// screen remembers the key of every record any wait has seen (start + each reload) and an
// automatic wait never starts again for a remembered key.
export function waitKey(d) {
  const fp = fpOf(d)
  return `${fp.state}|${fp.updated_at}`
}

// A new automatic wait for this detail, or null. `seen` = every record key any wait has seen
// (a Set; a single key string is accepted too), so the same record is waited on at most once,
// even when an older response shows it again (always bounded).
export function autoSendWait(d, seen) {
  if (!shouldWaitForSend(d)) return null
  const key = waitKey(d)
  const known = seen instanceof Set ? seen.has(key) : key === seen
  return known ? null : { since: fpOf(d).updated_at ?? null, polls: 0, errors: 0, auto: true, key }
}

// A wait after a manual Send or a link (the caller knows a send was just queued).
export function manualSendWait(d, auto = false) {
  return { since: fpOf(d).updated_at ?? null, polls: 0, errors: 0, auto, key: waitKey(d) }
}

// F-113: a reload that fails still counts as a poll and the wait goes on; after
// SEND_WAIT_MAX_ERRORS failures IN A ROW it stops with a visible note (manual Send re-enabled).
export const SEND_WAIT_MAX_ERRORS = 3

// One reload during a wait -> { done, polls, errors, note }. `error` = the failed reload (data
// is then ignored). Done when the record changed and is no longer busy, after
// SEND_WAIT_MAX_POLLS reloads (manual waits then say so), or after SEND_WAIT_MAX_ERRORS failed
// reloads in a row (always says so).
export function sendWaitStep(wait, data, error = null) {
  const polls = (wait && wait.polls ? wait.polls : 0) + 1
  if (error) {
    const errors = (wait && wait.errors ? wait.errors : 0) + 1
    if (errors >= SEND_WAIT_MAX_ERRORS || polls >= SEND_WAIT_MAX_POLLS) {
      const why = (error && error.message) || String(error)
      return { done: true, polls, errors,
        note: `Couldn't check the send status (${why}). Press Refresh to see the latest.` }
    }
    return { done: false, polls, errors, note: null }
  }
  const fp = fpOf(data)
  if ((fp.updated_at ?? null) !== (wait ? wait.since : null) && !fpBusy(fp)) {
    const problem = fp.state === 'attention' || fp.state === 'refused'
    return { done: true, polls, errors: 0, note: problem ? 'Finished with a problem (see above).' : null }
  }
  if (polls >= SEND_WAIT_MAX_POLLS) {
    return { done: true, polls, errors: 0,
      note: wait && wait.auto ? null : 'Still waiting for the worker. Refresh this page in a minute.' }
  }
  return { done: false, polls, errors: 0, note: null }
}
