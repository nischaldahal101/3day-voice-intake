import { useEffect, useState } from 'react'
import { fetchCartAddress, fetchCarts } from '../lib/api.js'
import { fmtDateTime, str } from '../lib/admin.js'
import { cartBadge, cartMoney, count, fpStateLabel } from '../lib/carts.js'

const REFRESH_MS = 10000
const PAGE = 50

function cssToken(s) {
  return String(s || '').replace(/[^a-z_]/g, '')
}

// "Your cart address" card: the user's own inbound address + Copy + how to use it.
function AddressCard() {
  const [address, setAddress] = useState(null)
  const [error, setError] = useState(null)
  const [copied, setCopied] = useState(false)
  const [tries, setTries] = useState(0)

  useEffect(() => {
    let cancelled = false
    fetchCartAddress()
      .then((d) => { if (!cancelled) { setAddress(d.address); setError(null) } })
      .catch((e) => { if (!cancelled) setError((e && e.message) || String(e)) })
    return () => { cancelled = true }
  }, [tries])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(address)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch (_) {
      setCopied(false)
      window.prompt('Copy your cart address:', address)
    }
  }

  return (
    <div className="admin-card cart-address">
      <h3 className="admin-card-title">Your cart address</h3>
      {address ? (
        <div className="cart-address-row">
          <code className="cart-address-text">{address}</code>
          <button className="btn btn-secondary btn-row-delete" onClick={copy}>
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      ) : error ? (
        <p className="admin-note is-error">
          {error}{' '}
          <button className="btn btn-tertiary btn-row-delete" onClick={() => setTries((n) => n + 1)}>Retry</button>
        </p>
      ) : (
        <p className="admin-note">Loading…</p>
      )}
      <p className="admin-note">
        In Ferguson Home, use <strong>Email project cart</strong> and send it to this address (or
        forward the cart email from your own inbox). The cart shows up here within a minute.
      </p>
    </div>
  )
}

// Ferguson carts (stage E2): the signed-in user's own carts, newest first. Refreshes
// itself every 10 s while open (new carts arrive by email at any time).
export default function CartList({ onBack, onOpen }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [offset, setOffset] = useState(0)
  const [reload, setReload] = useState(0)
  const [loads, setLoads] = useState(0)

  useEffect(() => {
    let cancelled = false
    fetchCarts({ offset, limit: PAGE })
      .then((d) => { if (!cancelled) { setData(d); setError(null) } })
      .catch((e) => { if (!cancelled) setError((e && e.message) || String(e)) })
      .finally(() => { if (!cancelled) setLoads((n) => n + 1) })
    return () => { cancelled = true }
  }, [offset, reload])

  useEffect(() => {
    const t = setTimeout(() => setReload((n) => n + 1), REFRESH_MS)
    return () => clearTimeout(t)
  }, [loads])

  const items = data && Array.isArray(data.items)
    ? data.items.filter((it) => it && typeof it === 'object' && typeof it.id === 'string')
    : null
  const total = data && typeof data.total === 'number' ? data.total : 0

  return (
    <section className="screen screen-admin screen-documents screen-carts">
      <header className="topbar">
        <button className="topbar-back" onClick={onBack} aria-label="Back">←</button>
        <div className="topbar-text">
          <span className="topbar-eyebrow">Ferguson Home</span>
          <h2 className="topbar-title">Carts</h2>
        </div>
      </header>

      <div className="admin-body">
        <AddressCard />

        <div className="admin-tools cart-tools">
          <button className="btn btn-tertiary admin-refresh" onClick={() => setReload((n) => n + 1)}>
            Refresh
          </button>
        </div>

        {error && !items && (
          <div className="state error">
            <p><strong>Couldn&rsquo;t load carts.</strong></p>
            <p className="state-detail">{error}</p>
            <button className="btn btn-secondary" onClick={() => setReload((n) => n + 1)}>Retry</button>
          </div>
        )}
        {error && items && <p className="admin-note is-error">Refresh failed: {error}</p>}
        {!error && !items && <div className="state loading">Loading…</div>}
        {items && items.length === 0 && offset === 0 && (
          <div className="state empty">
            <p className="empty-msg">No carts yet. Email a Ferguson Home project cart to your address above.</p>
          </div>
        )}
        {items && items.length > 0 && (
          <div className="admin-table cart-table" role="table" aria-label="Ferguson carts">
            <div className="admin-tr admin-th" role="row">
              <span role="columnheader">Received</span>
              <span role="columnheader">Project</span>
              <span role="columnheader">Items</span>
              <span role="columnheader">Subtotal</span>
              <span role="columnheader">Status</span>
            </div>
            {items.map((it) => {
              const n = count(it.item_count)
              const warns = count(it.warning_count) || 0
              return (
                <button key={it.id} className="admin-tr admin-row" role="row" onClick={() => onOpen(it.id)}>
                  <span className="admin-td admin-td-date" role="cell">
                    {fmtDateTime(it.received_at) || '—'}
                  </span>
                  <span className="admin-td admin-td-name" role="cell">
                    {str(it.project_name) || <em className="muted">No project name</em>}
                  </span>
                  <span className="admin-td" role="cell">
                    <span className="admin-cell-label">Items </span>
                    {n !== null ? n : '—'}
                  </span>
                  <span className="admin-td" role="cell">
                    <span className="admin-cell-label">Subtotal </span>
                    {cartMoney(it.subtotal)}
                  </span>
                  <span className="admin-td" role="cell">
                    <span className={`pill cart-status cart-status-${cssToken(it.status) || 'unknown'}`}>
                      {cartBadge(it.status, it.customer_name)}
                    </span>
                    {it.status !== 'needs_customer' && (
                      <span className={`pill fp-list fp-list-${cssToken(it.fieldpulse_state) || 'not_sent'}`}
                        title="FieldPulse estimate + material list">
                        FieldPulse: {fpStateLabel(it.fieldpulse_state)}
                      </span>
                    )}
                    {it.ok === false && (
                      <span className="pill doc-warn-badge" title="This cart couldn't be read completely">
                        Check cart
                      </span>
                    )}
                    {warns > 0 && (
                      <span className="pill doc-warn-badge" title="Some things could not be read">
                        {warns} warning{warns === 1 ? '' : 's'}
                      </span>
                    )}
                  </span>
                </button>
              )
            })}
          </div>
        )}
        {total > PAGE && (
          <div className="admin-pager">
            <button className="btn btn-tertiary" disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE))}>Newer</button>
            <span className="admin-pager-text">{offset + 1}–{Math.min(offset + PAGE, total)} of {total}</span>
            <button className="btn btn-tertiary" disabled={offset + PAGE >= total}
              onClick={() => setOffset(offset + PAGE)}>Older</button>
          </div>
        )}
      </div>
    </section>
  )
}
