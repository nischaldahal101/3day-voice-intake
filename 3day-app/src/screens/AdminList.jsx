import { useEffect, useRef, useState } from 'react'
import { fetchAdminConsultations } from '../lib/api.js'
import { FP_FILTERS, FP_LABEL, SOURCE_LABEL, fmtDateTime, str } from '../lib/admin.js'

const PAGE = 50

function fpLabel(item, labels) {
  return (labels && str(labels[item.fieldpulse_status])) ||
    FP_LABEL[item.fieldpulse_status] || str(item.fieldpulse_status) || 'Unknown'
}

function cssToken(s) {
  return String(s || '').replace(/[^a-z_]/g, '')
}

// Every consultation, newest first (admin panel list). Search by customer
// name, filter by FieldPulse status; 50 per page.
export default function AdminList({ onBack, onOpen, initial }) {
  const [q, setQ] = useState((initial && initial.q) || '')
  const [fp, setFp] = useState((initial && initial.fp) || '')
  const [offset, setOffset] = useState((initial && initial.offset) || 0)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [reload, setReload] = useState(0)
  const debounced = useRef(null)
  const [query, setQuery] = useState(q)

  // Debounce typing so every keystroke isn't a request.
  useEffect(() => {
    clearTimeout(debounced.current)
    debounced.current = setTimeout(() => setQuery(q.trim()), 300)
    return () => clearTimeout(debounced.current)
  }, [q])

  useEffect(() => {
    let cancelled = false
    setError(null)
    fetchAdminConsultations({ q: query, fp, offset, limit: PAGE })
      .then((d) => { if (!cancelled) setData(d) })
      .catch((e) => { if (!cancelled) setError((e && e.message) || String(e)) })
    return () => { cancelled = true }
  }, [query, fp, offset, reload])

  const items = data && Array.isArray(data.items)
    ? data.items.filter((it) => it && typeof it === 'object' && typeof it.id === 'string')
    : null
  const total = data && Number.isFinite(data.total) ? data.total : 0
  const counts = (data && data.counts && typeof data.counts === 'object') ? data.counts : {}
  const open = (id) => onOpen(id, { q, fp, offset })

  return (
    <section className="screen screen-admin">
      <header className="topbar">
        <button className="topbar-back" onClick={onBack} aria-label="Back">←</button>
        <div className="topbar-text">
          <span className="topbar-eyebrow">Admin</span>
          <h2 className="topbar-title">All consultations</h2>
        </div>
      </header>

      <div className="admin-tools">
        <input
          className="search-input"
          type="search"
          placeholder="Search by customer name"
          value={q}
          onChange={(e) => { setQ(e.target.value); setOffset(0) }}
          aria-label="Search by customer name"
        />
        <select
          className="admin-select"
          value={fp}
          onChange={(e) => { setFp(e.target.value); setOffset(0) }}
          aria-label="Filter by FieldPulse status"
        >
          {FP_FILTERS.map(([v, label]) => (
            <option key={v || 'all'} value={v}>
              {label}{counts[v || 'all'] != null ? ` (${counts[v || 'all']})` : ''}
            </option>
          ))}
        </select>
        <button className="btn btn-tertiary admin-refresh" onClick={() => setReload((n) => n + 1)}>
          Refresh
        </button>
      </div>

      <div className="admin-body">
        {error && (
          <div className="state error">
            <p><strong>Couldn&rsquo;t load consultations.</strong></p>
            <p className="state-detail">{error}</p>
            <button className="btn btn-secondary" onClick={() => setReload((n) => n + 1)}>Retry</button>
          </div>
        )}
        {!error && !items && <div className="state loading">Loading…</div>}
        {!error && items && items.length === 0 && (
          <div className="state empty"><p className="empty-msg">No consultations match.</p></div>
        )}
        {!error && items && items.length > 0 && (
          <>
            <div className="admin-table" role="table" aria-label="Consultations">
              <div className="admin-tr admin-th" role="row">
                <span role="columnheader">Date</span>
                <span role="columnheader">Customer</span>
                <span role="columnheader">Source</span>
                <span role="columnheader">Job</span>
                <span role="columnheader">FieldPulse</span>
                <span role="columnheader">Close</span>
              </div>
              {items.map((it) => (
                <button
                  key={it.id}
                  className={`admin-tr admin-row${it.needs_attention ? ' is-attention' : ''}`}
                  role="row"
                  onClick={() => open(it.id)}
                >
                  <span className="admin-td admin-td-date" role="cell">
                    {fmtDateTime(it.created_at) || '—'}
                  </span>
                  <span className="admin-td admin-td-name" role="cell">
                    {str(it.customer_name) || <em className="muted">No customer</em>}
                    {str(it.project_type) && it.project_type !== 'unknown' && (
                      <span className="admin-sub"> · {it.project_type}</span>
                    )}
                  </span>
                  <span className="admin-td" role="cell">
                    <span className="admin-cell-label">Source </span>
                    {SOURCE_LABEL[it.source] || str(it.source) || '—'}
                  </span>
                  <span className="admin-td" role="cell">
                    <span className={`pill job-status status-${cssToken(it.status)}`}>
                      {str(it.status) || '?'}
                    </span>
                  </span>
                  <span className="admin-td" role="cell">
                    <span className={`pill fp-pill fp-${cssToken(it.fieldpulse_status)}`}>
                      {fpLabel(it, data.labels)}
                    </span>
                    {it.manual_send && it.manual_send.status === 'queued' && (
                      <span className="admin-sub"> · send queued</span>
                    )}
                  </span>
                  <span className="admin-td admin-td-score" role="cell">
                    <span className="admin-cell-label">Close score </span>
                    {typeof it.close_score === 'number' ? `${it.close_score}/10` : '—'}
                  </span>
                </button>
              ))}
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
            {data.truncated && (
              <p className="admin-note">
                Showing the newest {data.scan_limit} consultations. Older ones are still in the
                database.
              </p>
            )}
          </>
        )}
      </div>
    </section>
  )
}
