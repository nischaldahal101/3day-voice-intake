import { useEffect, useMemo, useState } from 'react'
import { fetchCustomers } from '../lib/api.js'

export default function CustomerPicker({ onBack, onSelect, onAddNew }) {
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
        <button
          type="button"
          className="add-customer-cta"
          onClick={onAddNew}
        >
          <span className="add-customer-plus" aria-hidden>+</span>
          <span>Add new customer</span>
        </button>
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
              No customers match &ldquo;{query}&rdquo;
            </p>
            <button className="btn btn-tertiary" onClick={onAddNew}>
              + Add a new customer
            </button>
          </div>
        )}
        {customers && filtered.length > 0 && (
          <ul className="customer-list">
            {filtered.map((c) => {
              const name =
                c.display_name ||
                `${c.first_name || ''} ${c.last_name || ''}`.trim() ||
                '(unnamed)'
              return (
                <li key={c.id}>
                  <button
                    className="customer-row"
                    onClick={() => onSelect(c)}
                  >
                    <span className="customer-name">{name}</span>
                    <span className="customer-meta">
                      {c.phone || '—'}
                      {c.email ? <span className="dot">·</span> : null}
                      {c.email || ''}
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
