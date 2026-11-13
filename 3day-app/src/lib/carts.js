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
