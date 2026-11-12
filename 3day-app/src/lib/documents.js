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

// May this document be deleted now? Same rule as the server (documents_panel._can_delete):
// not while queued/processing, unless stuck. Uses the server's can_delete when present.
export function canDeleteDocument(doc) {
  if (!doc || typeof doc !== 'object') return false
  if (typeof doc.can_delete === 'boolean') return doc.can_delete
  return !BUSY.has(doc.status) || doc.stuck === true
}

// The confirm-dialog text for deleting one document (detail screen and customer page).
export function deleteDocumentPrompt(filename, customerName) {
  const f = typeof filename === 'string' && filename.trim() ? filename : 'unknown'
  const c = typeof customerName === 'string' && customerName.trim() ? customerName : 'none'
  return 'Delete this vendor document?\n\n' +
    `File: ${f}\n` +
    `Customer: ${c}\n\n` +
    'This removes the PDF and what was read from it from this app. It cannot be undone.'
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

// FieldPulse status of a customer's combined vendor list (documents_panel.list_status).
export const LIST_STATUS_LABEL = {
  not_sent: 'Not sent',
  dry_run: 'Dry run',
  held: 'Held (dry run)',
  sent: 'Sent',
  attention: 'Needs attention',
  sending: 'Sending…',
}

export function listStatusLabel(key) {
  return (typeof key === 'string' && LIST_STATUS_LABEL[key]) || LIST_STATUS_LABEL.not_sent
}

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : 0)

// Keep auto-refreshing a customers-list row while a document is being read, its
// FieldPulse list is being sent, or a finished document's automatic sync is still on
// its way (server: sync_pending). F-80: reading alone stopped too early.
export function customerRowNeedsRefresh(it) {
  if (!it || typeof it !== 'object') return false
  return num(it.processing_count) > 0 || it.sync_pending === true || it.fieldpulse_status === 'sending'
}

// Same for the customer page (fieldpulse.status instead of fieldpulse_status).
export function customerPageNeedsRefresh(d) {
  if (!d || typeof d !== 'object') return false
  const fp = d.fieldpulse && typeof d.fieldpulse === 'object' ? d.fieldpulse : {}
  return num(d.processing_count) > 0 || d.sync_pending === true || fp.status === 'sending'
}

// #documents (customers), #documents/c/<customer_id>, #documents/<doc_id>, else null.
export function parseDocumentsHash(hash) {
  const h = typeof hash === 'string' ? hash.replace(/^#\/?/, '') : ''
  if (/^documents\/?$/.test(h)) return { view: 'list' }
  const c = /^documents\/c\/([A-Za-z0-9_]{1,64})$/.exec(h)
  if (c) return { view: 'customer', customerId: c[1] }
  const m = /^documents\/([A-Za-z0-9_-]{1,64})$/.exec(h)
  if (m) return { view: 'detail', docId: m[1] }
  return null
}
