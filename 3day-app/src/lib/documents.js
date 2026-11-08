// Vendor documents display helpers (stage P1b). Pure: no network, no React state.
// Every helper re-guards server JSON (any field can be missing or odd).

export const DASH = '—'

export const DOC_TYPE_LABEL = {
  selection: 'Selection',
  quote: 'Quote',
  order: 'Order',
  receipt: 'Receipt',
  other: 'Other',
}

export const DOC_STATUS_LABEL = {
  queued: 'Waiting',
  processing: 'Reading…',
  done: 'Done',
  failed: 'Failed',
}

const BUSY = new Set(['queued', 'processing'])

// Still being read (and not a stuck leftover from a dead worker).
export function isReading(doc) {
  return !!doc && BUSY.has(doc.status) && !doc.stuck
}

export function docTypeLabel(t) {
  return (typeof t === 'string' && DOC_TYPE_LABEL[t]) || DOC_TYPE_LABEL.other
}

// Text or "—" (null / empty / non-string).
export function textOr(v) {
  if (typeof v === 'string' && v.trim()) return v
  if (typeof v === 'number' && Number.isFinite(v)) return String(v)
  return DASH
}

// $1,234.56 (up to 3 decimals, as printed on some quotes) or "—".
export function money(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return DASH
  return v.toLocaleString('en-US', {
    style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 3,
  })
}

export function qty(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return DASH
  return v.toLocaleString('en-US', { maximumFractionDigits: 3 })
}

export function count(v) {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null
}

// #documents and #documents/<id>, else null.
export function parseDocumentsHash(hash) {
  const h = typeof hash === 'string' ? hash.replace(/^#\/?/, '') : ''
  if (/^documents\/?$/.test(h)) return { view: 'list' }
  const m = /^documents\/([A-Za-z0-9_-]{1,64})$/.exec(h)
  if (m) return { view: 'detail', docId: m[1] }
  return null
}
