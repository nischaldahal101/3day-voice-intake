import { useEffect, useMemo, useState } from 'react'
import { fetchCustomers } from '../lib/api.js'

const DATE_FORMAT = { month: 'short', day: 'numeric', year: 'numeric' }

// "Added Nov 3, 2026" from the server's created_at; null if missing/bad.
// A date-only value ("2026-11-03") is a calendar date: build it as a local
// date so it never shifts a day. A full timestamp is shown in local time.
function addedLabel(createdAt) {
  if (typeof createdAt !== 'string' || !createdAt) return null
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(createdAt)
  const d = m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : new Date(createdAt)
  if (Number.isNaN(d.getTime())) return null
  return `Added ${d.toLocaleDateString(undefined, DATE_FORMAT)}`
}

// Read-only picker. The list keeps the server's order (newest added first);
// search only filters it. Customers are created in FieldPulse, not here.
export default function CustomerPicker({ onBack, onSelect }) {
  const [customers, setCustomers] = useState(null) // null = loading
  const [error, setError] = useState(null)
  const [query, setQuery] = useState('')

  const load = () => {
    setCustomers(null)
    setError(null)
    fetchCustomers()
      .then(setCustomers)
      .catch((e) => setError((e && e.message) || String(e)))
  }

  useEffect(() => {
    let cancelled = false
    fetchCustomers()
      .then((data) => {
        if (!cancelled) setCustomers(data)
      })
      .catch((e) => {
        if (!cancelled) setError((e && e.message) || String(e))
      })
    return () => {
      cancelled = true
    }
  }, [])

  const filtered = useMemo(() => {
    if (!customers) return []
    const q = query.trim().toLowerCase()
    if (!q) return customers
    return customers.filter((c) => {
      const hay = [c.display_name, c.first_name, c.last_name, c.phone, c.email]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      return hay.includes(q)
    })
  }, [customers, query])

  return (
    <section className="screen screen-pick">
      <header className="topbar">
        <button className="topbar-back" onClick={onBack} aria-label="Back">
          ←
        </button>
        <div className="topbar-text">
          <span className="topbar-eyebrow">Step 1 of 2</span>
          <h2 className="topbar-title">Choose a customer</h2>
        </div>
      </header>
      <div className="search-row">
        <input
          className="search-input"
          type="search"
          placeholder="Search by name, phone, or email"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          autoCapitalize="words"
          autoCorrect="off"
        />
      </div>
      <div className="list-wrap">
        {customers === null && !error && (
          <div className="state loading">Loading customers…</div>
        )}
        {error && (
          <div className="state error">
            <p>
              <strong>Couldn&rsquo;t load customers.</strong>
            </p>
            <p className="state-detail">{error}</p>
            <button className="btn btn-secondary" onClick={load}>
              Retry
            </button>
          </div>
        )}
        {customers && filtered.length === 0 && !error && (
          <div className="state empty">
            <p className="empty-msg">
              {customers.length === 0
                ? 'No customers yet.'
                : <>No customers match &ldquo;{query}&rdquo;.</>}
              {' '}Customers are added in FieldPulse.
            </p>
          </div>
        )}
        {customers && filtered.length > 0 && (
          <ul className="customer-list">
            {filtered.map((c) => {
              const added = addedLabel(c.created_at)
              const name =
                c.display_name ||
                `${c.first_name || ''} ${c.last_name || ''}`.trim() ||
                '(unnamed)'
              return (
                <li key={String(c.id)}>
                  <button
                    className="customer-row"
                    onClick={() => onSelect(c)}
                  >
                    <span className="customer-name">{name}</span>
                    <span className="customer-meta">
                      {c.phone || '—'}
                      {c.email ? <span className="dot">·</span> : null}
                      {c.email || ''}
                      {added ? <span className="dot">·</span> : null}
                      {added || ''}
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </section>
  )
}
