import { useEffect, useState } from 'react'
import { fetchDocuments } from '../lib/api.js'
import { fmtDateTime, str } from '../lib/admin.js'
import { DOC_STATUS_LABEL, count, docTypeLabel, isReading } from '../lib/documents.js'

const PAGE = 50
const REFRESH_MS = 5000

function cssToken(s) {
  return String(s || '').replace(/[^a-z_]/g, '')
}

// Every vendor document, newest first. Refreshes itself every 5 s while any
// row on the page is still being read.
export default function DocumentsList({ onBack, onOpen, onUpload }) {
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [reload, setReload] = useState(0)
  const [loads, setLoads] = useState(0) // finished loads (ok or not): drives auto-refresh

  useEffect(() => {
    let cancelled = false
    fetchDocuments({ offset, limit: PAGE })
      .then((d) => { if (!cancelled) { setData(d); setError(null) } })
      .catch((e) => { if (!cancelled) setError((e && e.message) || String(e)) })
      .finally(() => { if (!cancelled) setLoads((n) => n + 1) })
    return () => { cancelled = true }
  }, [offset, reload])

  const items = data && Array.isArray(data.items)
    ? data.items.filter((it) => it && typeof it === 'object' && typeof it.id === 'string')
    : null
  const total = data && Number.isFinite(data.total) ? data.total : 0
  const reading = !!items && items.some(isReading)

  // Auto-refresh while something is being read (a failed refresh just tries again).
  useEffect(() => {
    if (!reading) return undefined
    const t = setTimeout(() => setReload((n) => n + 1), REFRESH_MS)
    return () => clearTimeout(t)
  }, [reading, loads])

  // The page can be past the end after a delete: step back.
  const pastEnd = !!items && items.length === 0 && offset > 0
  useEffect(() => {
    if (pastEnd) setOffset((o) => Math.max(0, o - PAGE))
  }, [pastEnd])

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
            <p><strong>Couldn&rsquo;t load documents.</strong></p>
            <p className="state-detail">{error}</p>
            <button className="btn btn-secondary" onClick={() => setReload((n) => n + 1)}>Retry</button>
          </div>
        )}
        {error && items && <p className="admin-note is-error">Refresh failed: {error}</p>}
        {!error && !items && <div className="state loading">Loading…</div>}
        {items && items.length === 0 && !pastEnd && (
          <div className="state empty">
            <p className="empty-msg">No vendor documents yet. Upload a quote, order or receipt PDF.</p>
          </div>
        )}
        {items && items.length > 0 && (
          <>
            <div className="admin-table doc-table" role="table" aria-label="Vendor documents">
              <div className="admin-tr admin-th" role="row">
                <span role="columnheader">Date</span>
                <span role="columnheader">Customer</span>
                <span role="columnheader">Vendor</span>
                <span role="columnheader">Type</span>
                <span role="columnheader">File</span>
                <span role="columnheader">Status</span>
                <span role="columnheader">Lines</span>
              </div>
              {items.map((it) => {
                const lines = count(it.line_count)
                const warns = count(it.warn_count)
                return (
                  <button
                    key={it.id}
                    className={`admin-tr admin-row${it.status === 'failed' ? ' is-attention' : ''}`}
                    role="row"
                    onClick={() => onOpen(it.id)}
                  >
                    <span className="admin-td admin-td-date" role="cell">
                      {fmtDateTime(it.created_at) || '—'}
                    </span>
                    <span className="admin-td admin-td-name" role="cell">
                      {str(it.customer_name) || <em className="muted">No customer</em>}
                    </span>
                    <span className="admin-td" role="cell">
                      <span className="admin-cell-label">Vendor </span>
                      {str(it.vendor) || '—'}
                    </span>
                    <span className="admin-td" role="cell">
                      <span className="admin-cell-label">Type </span>
                      {it.status === 'done' ? docTypeLabel(it.document_type) : '—'}
                    </span>
                    <span className="admin-td doc-td-file" role="cell">
                      <span className="admin-cell-label">File </span>
                      {str(it.filename) || '—'}
                    </span>
                    <span className="admin-td" role="cell">
                      <span className={`pill job-status status-${cssToken(it.status)}`}>
                        {DOC_STATUS_LABEL[it.status] || str(it.status) || '?'}
                      </span>
                      {it.stuck && <span className="admin-sub"> · stuck</span>}
                    </span>
                    <span className="admin-td" role="cell">
                      <span className="admin-cell-label">Lines </span>
                      {lines !== null ? lines : '—'}
                      {warns > 0 && (
                        <span className="pill doc-warn-badge" title="Checks have warnings">
                          {warns} warning{warns === 1 ? '' : 's'}
                        </span>
                      )}
                    </span>
                  </button>
                )
              })}
            </div>
            <div className="admin-pager">
              <button
                className="btn btn-tertiary"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE))}
              >
                ← Newer
              </button>
              <span className="admin-pager-text">
                {offset + 1}–{Math.min(offset + items.length, total)} of {total}
              </span>
              <button
                className="btn btn-tertiary"
                disabled={offset + items.length >= total}
                onClick={() => setOffset(offset + PAGE)}
              >
                Older →
              </button>
            </div>
          </>
        )}
      </div>
    </section>
  )
}
