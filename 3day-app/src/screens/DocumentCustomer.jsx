import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchDocumentCustomer, requestVendorListSend } from '../lib/api.js'
import { fmtDateTime, isObj, str } from '../lib/admin.js'
import {
  DASH, DOC_STATUS_LABEL, count, customerPageNeedsRefresh, docTypeLabel, listStatusLabel, money,
  qty, textOr,
} from '../lib/documents.js'
import CustomerProfileCard from './CustomerProfileCard.jsx'

const REFRESH_MS = 5000
const SEND_POLL_MS = 3000
const SEND_POLL_MAX = 40 // ~2 min waiting for the worker to record the send

function cssToken(s) {
  return String(s || '').replace(/[^a-z_]/g, '')
}

function FieldPulseCard({ fp, onSend, onRefresh, sending, sendNote }) {
  const f = isObj(fp) ? fp : {}
  const attention = f.status === 'attention'
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">
        FieldPulse material list
        <span className={`pill fp-list fp-list-${cssToken(f.status) || 'not_sent'}`}>
          {listStatusLabel(f.status)}
        </span>
      </h3>
      <p className="admin-note">
        One list for this customer, &ldquo;Vendor materials&rdquo;, built from all their read
        documents. It is replaced on every update; Mike&rsquo;s own material lists are never touched.
      </p>
      {f.list_id && <p className="admin-note">FieldPulse list id: {String(f.list_id)}</p>}
      {f.updated_at && <p className="admin-note">Last update: {fmtDateTime(f.updated_at)}{f.mode ? ` (${str(f.mode)})` : ''}</p>}
      {f.last_error && (
        <div className={attention ? 'admin-alert' : 'admin-note'} role={attention ? 'alert' : undefined}>
          {str(f.last_error)}
        </div>
      )}
      <div className="doc-cust-actions">
        <button className="btn btn-primary" onClick={onSend} disabled={sending}>
          {sending ? 'Sending…' : attention ? 'Retry' : 'Send to FieldPulse'}
        </button>
        <button className="btn btn-tertiary" onClick={onRefresh}>Refresh</button>
      </div>
      {sendNote && <p className="admin-note">{sendNote}</p>}
    </div>
  )
}

function Documents({ docs, onOpen }) {
  const list = Array.isArray(docs) ? docs.filter((d) => isObj(d) && typeof d.id === 'string') : []
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">Documents ({list.length})</h3>
      {!list.length ? <p className="admin-note">No documents.</p> : (
        <div className="admin-table doc-cust-docs" role="table" aria-label="Documents">
          <div className="admin-tr admin-th" role="row">
            <span role="columnheader">Date</span>
            <span role="columnheader">Vendor</span>
            <span role="columnheader">Type</span>
            <span role="columnheader">File</span>
            <span role="columnheader">Status</span>
            <span role="columnheader">Lines</span>
          </div>
          {list.map((d) => {
            const warns = count(d.warn_count) || 0
            const lines = count(d.line_count)
            return (
              <button key={d.id} role="row" onClick={() => onOpen(d.id)}
                className={`admin-tr admin-row${d.status === 'failed' ? ' is-attention' : ''}`}>
                <span className="admin-td admin-td-date" role="cell">{fmtDateTime(d.created_at) || DASH}</span>
                <span className="admin-td" role="cell"><span className="admin-cell-label">Vendor </span>{str(d.vendor) || DASH}</span>
                <span className="admin-td" role="cell"><span className="admin-cell-label">Type </span>{d.status === 'done' ? docTypeLabel(d.document_type) : DASH}</span>
                <span className="admin-td doc-td-file" role="cell"><span className="admin-cell-label">File </span>{str(d.filename) || DASH}</span>
                <span className="admin-td" role="cell">
                  <span className={`pill job-status status-${cssToken(d.status)}`}>
                    {DOC_STATUS_LABEL[d.status] || str(d.status) || '?'}
                  </span>
                  {d.stuck && <span className="admin-sub"> · stuck</span>}
                </span>
                <span className="admin-td" role="cell">
                  <span className="admin-cell-label">Lines </span>{lines !== null ? lines : DASH}
                  {warns > 0 && <span className="pill doc-warn-badge">{warns} warning{warns === 1 ? '' : 's'}</span>}
                </span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

function Preview({ preview }) {
  const p = isObj(preview) ? preview : {}
  const sections = Array.isArray(p.sections) ? p.sections.filter(isObj) : []
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">Combined material list</h3>
      <p className="admin-note">
        Exactly what is sent: <strong>{str(p.title) || DASH}</strong> ·{' '}
        {count(p.line_count) ?? 0} line{p.line_count === 1 ? '' : 's'} from{' '}
        {count(p.document_count) ?? 0} read document{p.document_count === 1 ? '' : 's'}.
      </p>
      {str(p.notes) && <p className="doc-list-notes">{p.notes}</p>}
      {!sections.length && <p className="admin-note">No lines yet.</p>}
      {sections.map((s, si) => {
        const lines = Array.isArray(s.lines) ? s.lines.filter(isObj) : []
        return (
          <div key={si}>
            <h4 className="doc-section-title">{str(s.vendor) || 'Other'} ({lines.length})</h4>
            <div className="doc-lines-wrap">
              <table className="doc-lines">
                <thead>
                  <tr>
                    <th>Item</th><th>SKU</th><th className="num">Qty</th>
                    <th className="num">Unit price</th><th>Flags</th><th>Description</th>
                    <th className="num">Docs</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.map((l, i) => {
                    const flags = Array.isArray(l.flags) ? l.flags.filter((f) => typeof f === 'string') : []
                    return (
                      <tr key={i}>
                        <td data-label="Item">{textOr(l.title)}</td>
                        <td data-label="SKU">{textOr(l.sku)}</td>
                        <td data-label="Qty" className="num">{qty(l.quantity)}</td>
                        <td data-label="Unit price" className="num">{money(l.unit_price)}</td>
                        <td data-label="Flags">
                          {flags.length ? flags.map((f) => <span key={f} className="doc-flag">{f}</span>) : DASH}
                        </td>
                        <td data-label="Description">{textOr(l.description)}</td>
                        <td data-label="Docs" className="num">{Array.isArray(l.sources) ? l.sources.length : DASH}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function Troubleshooting({ fp }) {
  const f = isObj(fp) ? fp : {}
  return (
    <details className="admin-trouble">
      <summary>Details for troubleshooting</summary>
      <div className="admin-card">
        <h3 className="admin-card-title">Last FieldPulse request</h3>
        <dl className="admin-fields">
          <div className="admin-field"><dt>Status</dt><dd>{textOr(f.raw_status)}</dd></div>
          <div className="admin-field"><dt>Mode</dt><dd>{textOr(f.mode)}</dd></div>
        </dl>
        <details className="admin-raw">
          <summary>Request</summary>
          <pre>{f.last_request ? JSON.stringify(f.last_request, null, 2) : DASH}</pre>
        </details>
        <details className="admin-raw">
          <summary>Response</summary>
          <pre>{f.last_response ? JSON.stringify(f.last_response, null, 2) : DASH}</pre>
        </details>
        <details className="admin-raw">
          <summary>Recent attempts</summary>
          <pre>{Array.isArray(f.history) && f.history.length ? JSON.stringify(f.history, null, 2) : DASH}</pre>
        </details>
      </div>
    </details>
  )
}

// Body (exported so it can be rendered with fixture data).
export function DocumentCustomerBody({ d, onOpenDocument, onUpload, onSend, onRefresh, sending, sendNote }) {
  return (
    <div className="admin-detail">
      <div className="admin-col">
        <CustomerProfileCard customer={d.customer} />
        <div className="admin-card">
          <h3 className="admin-card-title">Upload</h3>
          <p className="admin-note">Add another vendor PDF for this customer.</p>
          <button className="btn btn-secondary" onClick={onUpload} disabled={!isObj(d.customer)}>
            Upload document
          </button>
        </div>
      </div>
      <div className="admin-col">
        <FieldPulseCard fp={d.fieldpulse} onSend={onSend} onRefresh={onRefresh} sending={sending}
          sendNote={sendNote} />
      </div>
      <div className="admin-col admin-col-full">
        <Documents docs={d.documents} onOpen={onOpenDocument} />
        <Preview preview={d.preview} />
        <Troubleshooting fp={d.fieldpulse} />
      </div>
    </div>
  )
}

// One customer's vendor documents: who, FieldPulse list status + Send/Retry,
// upload for this customer, documents, the combined list preview.
export default function DocumentCustomer({ customerId, onBack, onOpenDocument, onUpload }) {
  const [d, setD] = useState(null)
  const [error, setError] = useState(null)
  const [tick, setTick] = useState(0)
  const [sending, setSending] = useState(false)
  const [sendNote, setSendNote] = useState(null)
  const mounted = useRef(true)
  const waitRef = useRef(null) // {since, polls} while a manual send is pending

  const load = useCallback(async () => {
    const data = await fetchDocumentCustomer(customerId)
    if (mounted.current) setD(data)
    return data
  }, [customerId])

  useEffect(() => {
    mounted.current = true
    setD(null)
    setError(null)
    load().catch((e) => {
      if (!mounted.current) return
      setError(e && e.status === 404 ? 'This customer has no vendor documents any more.'
        : (e && e.message) || String(e))
    })
    return () => { mounted.current = false }
  }, [load])

  // Refresh while a document is being read, an automatic list sync is on its way or
  // sending (customerPageNeedsRefresh, F-80), or a manual send is pending (until the
  // worker records it: fieldpulse.updated_at changes).
  const reading = customerPageNeedsRefresh(d)
  useEffect(() => {
    if (!d || (!reading && !sending)) return undefined
    const t = setTimeout(() => {
      load().then((data) => {
        const w = waitRef.current
        if (!w || !mounted.current) return
        const fp = isObj(data.fieldpulse) ? data.fieldpulse : {}
        w.polls += 1
        if (fp.updated_at !== w.since && fp.raw_status !== 'sending') {
          waitRef.current = null
          setSending(false)
          setSendNote(fp.status === 'attention' ? 'Finished with a problem (see above).'
            : `Done: ${listStatusLabel(fp.status)}.`)
        } else if (w.polls >= SEND_POLL_MAX) {
          waitRef.current = null
          setSending(false)
          setSendNote('Still waiting for the worker. Refresh this page in a minute.')
        }
      }).catch(() => { if (mounted.current) setTick((n) => n + 1) })
    }, sending ? SEND_POLL_MS : REFRESH_MS)
    return () => clearTimeout(t)
  }, [d, reading, sending, load, tick])

  const onSend = async () => {
    if (!d || sending) return
    const ok = window.confirm(
      'Send the combined vendor material list to FieldPulse?\n\n' +
      `Customer: ${str(d.customer_name) || customerId}\n` +
      `${count(d.preview && d.preview.line_count) ?? 0} line(s).\n\n` +
      'It creates (or replaces) the "Vendor materials" list the app made for this customer. ' +
      "The worker's write mode still decides: in a dry run nothing is sent.",
    )
    if (!ok) return
    setSending(true)
    setSendNote('Queued. Waiting for the worker…')
    try {
      await requestVendorListSend(customerId)
      waitRef.current = { since: isObj(d.fieldpulse) ? d.fieldpulse.updated_at : null, polls: 0 }
      setTick((n) => n + 1)
    } catch (e) {
      if (!mounted.current) return
      setSending(false)
      setSendNote((e && e.message) || String(e))
    }
  }

  return (
    <section className="screen screen-admin screen-documents">
      <header className="topbar">
        <button className="topbar-back" onClick={onBack} aria-label="Back">←</button>
        <div className="topbar-text">
          <span className="topbar-eyebrow">Vendor documents</span>
          <h2 className="topbar-title">{(d && str(d.customer_name)) || `Customer #${customerId}`}</h2>
        </div>
      </header>
      <div className="admin-body">
        {error && (
          <div className="state error">
            <p><strong>Couldn&rsquo;t load this customer.</strong></p>
            <p className="state-detail">{error}</p>
            <button className="btn btn-secondary" onClick={() => {
              setError(null)
              load().catch((e) => setError((e && e.message) || String(e)))
            }}>Retry</button>
          </div>
        )}
        {!error && !d && <div className="state loading">Loading…</div>}
        {!error && d && (
          <DocumentCustomerBody d={d} onOpenDocument={onOpenDocument}
            onUpload={() => onUpload(d.customer)} onSend={onSend} sending={sending} sendNote={sendNote}
            onRefresh={() => load().catch(() => setTick((n) => n + 1))} />
        )}
      </div>
    </section>
  )
}
