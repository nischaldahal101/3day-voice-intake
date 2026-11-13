import { useCallback, useEffect, useRef, useState } from 'react'
import { deleteCart, fetchCart, linkCartCustomer, requestCartSend, retryCartMatch } from '../lib/api.js'
import { fmtDateTime, isObj, str } from '../lib/admin.js'
import {
  DASH, cartBadge, cartMoney, deleteCartPrompt, expiryText, fpBusy, fpStateLabel, groupByRoom,
  itemTitle, itemTypeView, lineTotal, matchPending, matchText, qtyText, safeLink, sendButtonText, sendCartPrompt,
} from '../lib/carts.js'
import CustomerPicker from './CustomerPicker.jsx'

const POLL_MS = 3000
const MAX_POLLS = 40 // ~2 min while the automatic match runs

function Field({ label, value }) {
  return (
    <div className="admin-field">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )
}

function Header({ d }) {
  const c = isObj(d.cart) ? d.cart : {}
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">
        Cart
        <span className={`pill cart-status cart-status-${String(d.status || '').replace(/[^a-z_]/g, '') || 'unknown'}`}>
          {cartBadge(d.status, isObj(d.customer) ? d.customer.display_name : null)}
        </span>
      </h3>
      <dl className="admin-fields">
        <Field label="Project" value={str(d.project_name) || DASH} />
        <Field label="Pricing expires" value={expiryText(c.pricing_expires)} />
        <Field label="Subtotal" value={cartMoney(c.subtotal)} />
        <Field label="Received" value={fmtDateTime(d.received_at) || DASH} />
      </dl>
      {d.ok === false && (
        <p className="admin-note is-error">
          This cart couldn&rsquo;t be read completely. Check it before it is sent.
        </p>
      )}
    </div>
  )
}

// E3: which FieldPulse customer this cart belongs to, why, and Link / Change / Try again.
function CustomerCard({ d, busy, note, onPick, onRetry }) {
  const c = isObj(d.customer) ? d.customer : null
  const linked = d.status === 'linked' && c
  const text = matchText(d)
  const pending = matchPending(d)
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">FieldPulse customer</h3>
      {linked ? (
        <dl className="admin-fields">
          <Field label="Customer" value={str(c.display_name) || DASH} />
          <Field label="Phone" value={str(c.phone) || DASH} />
          <Field label="Address" value={str(c.address) || DASH} />
          <Field label="Linked" value={d.match === 'auto' ? 'Automatically, by name' : 'By hand'} />
        </dl>
      ) : (
        <p className="admin-note"><strong>Needs customer.</strong> Nothing is sent to FieldPulse until this
          cart is linked.</p>
      )}
      {text && <p className={d.match_state === 'error' ? 'admin-note is-error' : 'admin-note'}>{text}</p>}
      {d.can_link && (
        <button className="btn btn-secondary" disabled={busy} onClick={onPick}>
          {linked ? 'Change customer' : 'Link customer'}
        </button>
      )}
      {d.can_match && (
        <button className="btn btn-tertiary" disabled={busy || pending} onClick={onRetry}>
          {pending ? 'Matching…' : 'Try matching again'}
        </button>
      )}
      {d.relink_note && <p className="admin-note">{str(d.relink_note)}</p>}
      {note && <p className="admin-note is-error">{note}</p>}
    </div>
  )
}

function cssToken(s) {
  return String(s || '').replace(/[^a-z_]/g, '')
}

// E4: the cart's draft estimate + material list in FieldPulse, Send / Retry.
function FieldPulseCard({ d, onSend, onRefresh, sending, sendNote }) {
  const f = isObj(d.fieldpulse) ? d.fieldpulse : {}
  const attention = f.state === 'attention' || f.state === 'refused'
  const replacedBy = str(f.replaced_by)
  const text = str(f.problem) || str(f.message)
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">
        FieldPulse estimate + material list
        <span className={`pill fp-list fp-list-${cssToken(f.state) || 'not_sent'}`}>
          {fpStateLabel(f.state)}
        </span>
      </h3>
      {d.status === 'needs_customer' ? (
        <p className="admin-note">Link a customer first. Nothing is sent until the cart is linked.</p>
      ) : (
        <p className="admin-note">
          One DRAFT estimate and one material list &ldquo;Ferguson cart: {str(d.project_name) || '…'}&rdquo;
          with these lines. Forwarding the cart again for the same project replaces both (no second draft).
        </p>
      )}
      {replacedBy && (
        <p className="admin-note">
          <strong>Replaced by a newer cart.</strong> A newer cart for this project now owns this estimate and
          list. <a href={`#carts/${replacedBy}`}>Open the newer cart</a>.
        </p>
      )}
      {str(f.replaces) && <p className="admin-note">This cart replaced an <a href={`#carts/${str(f.replaces)}`}>earlier cart</a>.</p>}
      {(f.estimate_id || f.list_id) && (
        <dl className="admin-fields">
          <Field label="Estimate id" value={f.estimate_id ? String(f.estimate_id) : DASH} />
          <Field label="Material list id" value={f.list_id ? String(f.list_id) : DASH} />
        </dl>
      )}
      {f.updated_at && <p className="admin-note">Last update: {fmtDateTime(f.updated_at)}{f.mode ? ` (${str(f.mode)})` : ''}</p>}
      {text && f.state !== 'sent' && (
        <div className={attention ? 'admin-alert' : 'admin-note'} role={attention ? 'alert' : undefined}>
          {text}
        </div>
      )}
      <div className="doc-cust-actions">
        {f.can_send && (
          <button className="btn btn-primary" onClick={onSend} disabled={sending}>
            {sending ? 'Sending…' : sendButtonText(f.state)}
          </button>
        )}
        <button className="btn btn-tertiary" onClick={onRefresh}>Refresh</button>
      </div>
      {sendNote && <p className="admin-note">{sendNote}</p>}
    </div>
  )
}

function Troubleshooting({ fp }) {
  const f = isObj(fp) ? fp : {}
  const steps = isObj(f.steps) ? f.steps : {}
  return (
    <details className="admin-trouble">
      <summary>Details for troubleshooting</summary>
      <div className="admin-card">
        <h3 className="admin-card-title">Last FieldPulse requests</h3>
        <dl className="admin-fields">
          <Field label="Status" value={str(f.raw_status) || DASH} />
          <Field label="Mode" value={str(f.mode) || DASH} />
        </dl>
        {[['estimate', 'Estimate'], ['material_list', 'Material list']].map(([k, label]) => {
          const st = isObj(steps[k]) ? steps[k] : {}
          return (
            <div key={k}>
              <p className="admin-note"><strong>{label}:</strong> {str(st.status) || DASH}
                {st.message ? ` — ${str(st.message)}` : ''}</p>
              <details className="admin-raw">
                <summary>{label} request</summary>
                <pre>{st.request ? JSON.stringify(st.request, null, 2) : DASH}</pre>
              </details>
              <details className="admin-raw">
                <summary>{label} response</summary>
                <pre>{st.response ? JSON.stringify(st.response, null, 2) : DASH}</pre>
              </details>
            </div>
          )
        })}
        <details className="admin-raw">
          <summary>Recent attempts</summary>
          <pre>{Array.isArray(f.history) && f.history.length ? JSON.stringify(f.history, null, 2) : DASH}</pre>
        </details>
      </div>
    </details>
  )
}

function Warnings({ warnings }) {
  const list = Array.isArray(warnings) ? warnings.filter((w) => typeof w === 'string' && w.trim()) : []
  if (!list.length) return null
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">Check before using ({list.length})</h3>
      <ul className="doc-checks">
        {list.map((w, i) => (
          <li key={i} className="doc-check doc-check-warn">{w}</li>
        ))}
      </ul>
    </div>
  )
}

function Rooms({ items, typesInfo }) {
  const groups = groupByRoom(items)
  if (!groups.length) {
    return (
      <div className="admin-card">
        <h3 className="admin-card-title">Items</h3>
        <p className="admin-note">No items could be read from this email.</p>
      </div>
    )
  }
  return groups.map((g) => (
    <div className="admin-card" key={g.room}>
      <h3 className="admin-card-title">{g.room} ({g.items.length})</h3>
      <div className="doc-lines-wrap">
        <table className="doc-lines">
          <thead>
            <tr>
              <th>Item</th><th>Type</th><th>Finish</th><th className="num">Qty</th>
              <th className="num">Unit price</th><th className="num">Line total</th><th>Link</th>
            </tr>
          </thead>
          <tbody>
            {g.items.map((it, i) => {
              const href = safeLink(it.product_url)
              const type = itemTypeView(it, typesInfo)
              return (
                <tr key={i}>
                  <td data-label="Item">{itemTitle(it)}</td>
                  <td data-label="Type">
                    {type.flag ? <span className="cart-type-flag">{type.text}</span> : type.text}
                  </td>
                  <td data-label="Finish">{str(it.finish) || DASH}</td>
                  <td data-label="Qty" className="num">{qtyText(it.quantity)}</td>
                  <td data-label="Unit price" className="num">{cartMoney(it.unit_price)}</td>
                  <td data-label="Line total" className="num">{lineTotal(it.quantity, it.unit_price)}</td>
                  <td data-label="Link">
                    {href ? <a href={href} target="_blank" rel="noopener noreferrer">View</a> : DASH}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  ))
}

// One Ferguson cart: project, expiry, subtotal, FieldPulse customer (E3: link / change /
// try matching again), E4 FieldPulse send card (status, ids, Send/Retry), warnings, items by
// room, folded troubleshooting (exact requests/responses), Delete.
export default function CartDetail({ cartId, onBack, onDeleted }) {
  const [d, setD] = useState(null)
  const [error, setError] = useState(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteNote, setDeleteNote] = useState(null)
  const [picking, setPicking] = useState(false)
  const [busy, setBusy] = useState(false)       // linking or asking for a new match
  const [linkNote, setLinkNote] = useState(null)
  const [polls, setPolls] = useState(0)
  const [sending, setSending] = useState(false)
  const [sendNote, setSendNote] = useState(null)
  const sendWait = useRef(null)          // {since, polls} while waiting for the worker
  const mounted = useRef(true)

  const load = useCallback(async () => {
    const data = await fetchCart(cartId)
    if (mounted.current) setD(data)
    return data
  }, [cartId])

  useEffect(() => {
    mounted.current = true
    setD(null)
    setError(null)
    load().catch((e) => { if (mounted.current) setError((e && e.message) || String(e)) })
    return () => { mounted.current = false }
  }, [load])

  // While the automatic match runs, reload every 3 s (max ~2 min).
  useEffect(() => {
    if (!matchPending(d) || polls >= MAX_POLLS) return undefined
    const t = setTimeout(() => {
      setPolls((n) => n + 1)
      load().catch(() => { /* the next manual Refresh/Retry shows errors */ })
    }, POLL_MS)
    return () => clearTimeout(t)
  }, [d, polls, load])

  // After Send (or while the worker reports queued/sending): reload every 3 s until the
  // record's updated_at changes and it is no longer busy (max ~2 min).
  useEffect(() => {
    const w = sendWait.current
    // Without a pending Send, only a live 'sending' reloads (it turns into "Needs attention"
    // server-side after 5 min, so this always ends).
    const busy = !!d && isObj(d.fieldpulse) && d.fieldpulse.state === 'sending'
    if (!d || (!w && !busy)) return undefined
    const t = setTimeout(async () => {
      try {
        const data = await load()
        if (!mounted.current) return
        const fp = isObj(data.fieldpulse) ? data.fieldpulse : {}
        const cur = sendWait.current
        if (cur) {
          cur.polls += 1
          if (fp.updated_at !== cur.since && !fpBusy(fp)) {
            sendWait.current = null
            setSending(false)
            setSendNote(fp.state === 'attention' || fp.state === 'refused'
              ? 'Finished with a problem (see above).' : null)
          } else if (cur.polls >= MAX_POLLS) {
            sendWait.current = null
            setSending(false)
            setSendNote('Still waiting for the worker. Refresh this page in a minute.')
          }
        }
      } catch (_) { /* the Refresh button shows errors */ }
    }, POLL_MS)
    return () => clearTimeout(t)
  }, [d, load])

  const onSend = async () => {
    if (!d) return
    const name = isObj(d.customer) ? str(d.customer.display_name) : null
    if (!window.confirm(sendCartPrompt(str(d.project_name), name))) return
    setSending(true)
    setSendNote('Queued. Waiting for the worker…')
    try {
      await requestCartSend(cartId)
      if (!mounted.current) return
      sendWait.current = { since: isObj(d.fieldpulse) ? d.fieldpulse.updated_at : null, polls: 0 }
      setD({ ...d })                       // start the wait loop
    } catch (e) {
      if (!mounted.current) return
      setSending(false)
      setSendNote((e && e.message) || String(e))
    }
  }

  const onRefresh = () => {
    load().catch((e) => { if (mounted.current) setSendNote((e && e.message) || String(e)) })
  }

  const onPicked = async (c) => {
    setPicking(false)
    if (!c || c.id === undefined || c.id === null) return
    setBusy(true)
    setLinkNote(null)
    try {
      const data = await linkCartCustomer(cartId, c.id)
      if (mounted.current) setD(data)
    } catch (e) {
      if (mounted.current) setLinkNote((e && e.message) || String(e))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const onRetryMatch = async () => {
    setBusy(true)
    setLinkNote(null)
    try {
      await retryCartMatch(cartId)
      if (!mounted.current) return
      setPolls(0)
      await load()
    } catch (e) {
      if (mounted.current) setLinkNote((e && e.message) || String(e))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const onDelete = async () => {
    if (!d) return
    if (!window.confirm(deleteCartPrompt(str(d.project_name)))) return
    setDeleting(true)
    setDeleteNote(null)
    try {
      await deleteCart(cartId)
      if (mounted.current) onDeleted(cartId)
    } catch (e) {
      if (!mounted.current) return
      setDeleting(false)
      setDeleteNote((e && e.message) || String(e))
    }
  }

  if (picking) {
    // The same picker vendor documents use; back returns to this cart.
    return <CustomerPicker eyebrow="Link this cart to a customer" onBack={() => setPicking(false)}
      onSelect={onPicked} />
  }

  return (
    <section className="screen screen-admin screen-documents screen-carts">
      <header className="topbar">
        <button className="topbar-back" onClick={onBack} aria-label="Back">←</button>
        <div className="topbar-text">
          <span className="topbar-eyebrow">Ferguson cart</span>
          <h2 className="topbar-title">{(d && str(d.project_name)) || 'Ferguson cart'}</h2>
        </div>
      </header>
      <div className="admin-body">
        {error && (
          <div className="state error">
            <p><strong>Couldn&rsquo;t load this cart.</strong></p>
            <p className="state-detail">{error}</p>
            <button className="btn btn-secondary" onClick={() => {
              setError(null)
              load().catch((e) => setError((e && e.message) || String(e)))
            }}>Retry</button>
            <button className="btn btn-tertiary" onClick={onBack}>Back to list</button>
          </div>
        )}
        {!error && !d && <div className="state loading">Loading…</div>}
        {!error && d && (
          <div className="admin-detail">
            <div className="admin-col">
              <Header d={d} />
              <CustomerCard d={d} busy={busy || deleting} note={linkNote}
                onPick={() => { setLinkNote(null); setPicking(true) }} onRetry={onRetryMatch} />
            </div>
            <div className="admin-col">
              <FieldPulseCard d={d} onSend={onSend} onRefresh={onRefresh} sending={sending}
                sendNote={sendNote} />
              <Warnings warnings={d.warnings} />
            </div>
            <div className="admin-col admin-col-full">
              <Rooms items={isObj(d.cart) ? d.cart.items : []}
                     typesInfo={isObj(d.cart) && isObj(d.cart.item_types) ? d.cart.item_types : null} />
              <Troubleshooting fp={d.fieldpulse} />
              <div className="admin-card admin-delete">
                <h3 className="admin-card-title">Delete</h3>
                <p className="admin-note">Removes this cart (and the email it came from) from this app.</p>
                <button className="btn btn-danger" disabled={deleting} onClick={onDelete}>
                  {deleting ? 'Deleting…' : 'Delete cart'}
                </button>
                {deleteNote && <p className="admin-note is-error">{deleteNote}</p>}
              </div>
              <button className="btn btn-tertiary" onClick={onBack}>Back to list</button>
            </div>
          </div>
        )}
      </div>
    </section>
  )
}
