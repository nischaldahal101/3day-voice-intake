import { useEffect, useMemo, useState } from 'react'
import { fetchDocumentCustomers } from '../lib/api.js'
import { fmtDateTime, str } from '../lib/admin.js'
import { count, customerRowNeedsRefresh, listStatusLabel } from '../lib/documents.js'

const REFRESH_MS = 5000

function cssToken(s) {
  return String(s || '').replace(/[^a-z_]/g, '')
}

// Vendor documents, customer first (stage P2): every customer with documents,
// newest upload first, searchable by name. Refreshes itself every 5 s while any
// customer has a document being read or a FieldPulse list update on its way.
export default function DocumentCustomers({ onBack, onOpenCustomer, onUpload }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [q, setQ] = useState('')
  const [reload, setReload] = useState(0)
  const [loads, setLoads] = useState(0) // finished loads (ok or not): drives auto-refresh

  useEffect(() => {
    let cancelled = false
    fetchDocumentCustomers()
      .then((d) => { if (!cancelled) { setData(d); setError(null) } })
      .catch((e) => { if (!cancelled) setError((e && e.message) || String(e)) })
      .finally(() => { if (!cancelled) setLoads((n) => n + 1) })
    return () => { cancelled = true }
  }, [reload])

  const items = data && Array.isArray(data.items)
    ? data.items.filter((it) => it && typeof it === 'object' && typeof it.customer_id === 'string')
    : null
  const reading = !!items && items.some(customerRowNeedsRefresh)

  useEffect(() => {
    if (!reading) return undefined
    const t = setTimeout(() => setReload((n) => n + 1), REFRESH_MS)
    return () => clearTimeout(t)
  }, [reading, loads])

  const shown = useMemo(() => {
    if (!items) return null
    const needle = q.trim().toLowerCase()
    if (!needle) return items
    return items.filter((it) => (str(it.customer_name) || '').toLowerCase().includes(needle))
  }, [items, q])

  return (
    <section className="screen screen-admin screen-documents">
      <header className="topbar">
        <button className="topbar-back" onClick={onBack} aria-label="Back">←</button>
        <div className="topbar-text">
          <span className="topbar-eyebrow">Quotes, orders, receipts</span>
          <h2 className="topbar-title">Vendor documents</h2>
        </div>
      </header>

      <div className="admin-tools">
        <input
          className="search-input"
          type="search"
          placeholder="Search customers"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          aria-label="Search customers by name"
        />
        <button className="btn btn-primary doc-upload-btn" onClick={onUpload}>
          Upload document
        </button>
        <button className="btn btn-tertiary admin-refresh" onClick={() => setReload((n) => n + 1)}>
          Refresh
        </button>
      </div>

      <div className="admin-body">
        {error && !items && (
          <div className="state error">
            <p><strong>Couldn&rsquo;t load vendor documents.</strong></p>
            <p className="state-detail">{error}</p>
            <button className="btn btn-secondary" onClick={() => setReload((n) => n + 1)}>Retry</button>
          </div>
        )}
        {error && items && <p className="admin-note is-error">Refresh failed: {error}</p>}
        {!error && !items && <div className="state loading">Loading…</div>}
        {items && items.length === 0 && (
          <div className="state empty">
            <p className="empty-msg">No vendor documents yet. Upload a quote, order or receipt PDF.</p>
          </div>
        )}
        {shown && items.length > 0 && shown.length === 0 && (
          <p className="admin-note">No customer matches &ldquo;{q}&rdquo;.</p>
        )}
        {shown && shown.length > 0 && (
          <div className="admin-table doc-cust-table" role="table" aria-label="Customers with vendor documents">
            <div className="admin-tr admin-th" role="row">
              <span role="columnheader">Customer</span>
              <span role="columnheader">Documents</span>
              <span role="columnheader">Last upload</span>
              <span role="columnheader">FieldPulse list</span>
            </div>
            {shown.map((it) => {
              const docs = count(it.document_count)
              const busy = count(it.processing_count) || 0
              const warns = count(it.warn_count) || 0
              const fp = cssToken(it.fieldpulse_status)
              return (
                <button
                  key={it.customer_id}
                  className={`admin-tr admin-row${fp === 'attention' ? ' is-attention' : ''}`}
                  role="row"
                  onClick={() => onOpenCustomer(it.customer_id)}
                >
                  <span className="admin-td admin-td-name" role="cell">
                    {str(it.customer_name) || <em className="muted">Customer #{it.customer_id}</em>}
                  </span>
                  <span className="admin-td" role="cell">
                    <span className="admin-cell-label">Documents </span>
                    {docs !== null ? docs : '—'}
                    {busy > 0 && <span className="admin-sub"> · {busy} reading…</span>}
                    {warns > 0 && (
                      <span className="pill doc-warn-badge" title="Checks have warnings">
                        {warns} warning{warns === 1 ? '' : 's'}
                      </span>
                    )}
                  </span>
                  <span className="admin-td admin-td-date" role="cell">
                    <span className="admin-cell-label">Last upload </span>
                    {fmtDateTime(it.last_upload_at) || '—'}
                  </span>
                  <span className="admin-td" role="cell">
                    <span className={`pill fp-list fp-list-${fp || 'not_sent'}`}>
                      {listStatusLabel(it.fieldpulse_status)}
                    </span>
                  </span>
                </button>
              )
            })}
          </div>
        )}
      </div>
    </section>
  )
}
