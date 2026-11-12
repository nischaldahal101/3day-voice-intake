import { useCallback, useEffect, useRef, useState } from 'react'
import { deleteDocument, fetchDocument } from '../lib/api.js'
import { fmtDateTime, isObj, str } from '../lib/admin.js'
import {
  DASH, DOC_STATUS_LABEL, canDeleteDocument, deleteDocumentPrompt, docTypeLabel, isReading, money,
  qty, textOr,
} from '../lib/documents.js'

const REFRESH_MS = 5000
const CHECK_LABEL = { warn: 'Warning', ok: 'OK', info: 'Note' }

function Field({ label, value }) {
  return (
    <div className="admin-field">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )
}

function Header({ d }) {
  const r = isObj(d.result) ? d.result : {}
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">
        Document
        <span className={`pill job-status status-${String(d.status || '').replace(/[^a-z]/g, '')}`}>
          {DOC_STATUS_LABEL[d.status] || str(d.status) || '?'}
        </span>
      </h3>
      <dl className="admin-fields">
        <Field label="Vendor" value={textOr(r.vendor)} />
        <Field label="Type" value={d.result ? docTypeLabel(r.document_type) : DASH} />
        <Field label="Number" value={textOr(r.document_number)} />
        <Field label="Date" value={textOr(r.document_date)} />
        <Field label="Job name" value={textOr(r.job_name)} />
        <Field label="Customer" value={textOr(d.customer_name)} />
        <Field label="File" value={textOr(d.filename)} />
        <Field label="Uploaded" value={fmtDateTime(d.created_at) || DASH} />
      </dl>
      {d.status === 'failed' && (
        <div className="admin-alert" role="alert">
          <strong>Couldn&rsquo;t read this document.</strong> {str(d.error) || 'Unknown error.'}
        </div>
      )}
      {isReading(d) && <p className="admin-note">Reading the document… this page refreshes itself.</p>}
      {d.stuck && (
        <p className="admin-note is-error">
          This document has been waiting for a long time; the reader probably stopped. Delete it
          and upload it again.
        </p>
      )}
    </div>
  )
}

function Checks({ checks }) {
  const list = Array.isArray(checks) ? checks.filter(isObj) : []
  if (!list.length) return null
  const order = { warn: 0, info: 1, ok: 2 }
  const sorted = [...list].sort((a, b) => (order[a.level] ?? 3) - (order[b.level] ?? 3))
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">Checks</h3>
      <ul className="doc-checks">
        {sorted.map((c, i) => (
          <li key={i} className={`doc-check doc-check-${c.level === 'warn' ? 'warn' : c.level === 'ok' ? 'ok' : 'info'}`}>
            <strong>{CHECK_LABEL[c.level] || 'Note'}:</strong> {str(c.message) || str(c.code) || DASH}
          </li>
        ))}
      </ul>
    </div>
  )
}

function Lines({ lines }) {
  const list = Array.isArray(lines) ? lines.filter(isObj) : []
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">Lines ({list.length})</h3>
      {!list.length ? (
        <p className="admin-note">No product lines.</p>
      ) : (
        <div className="doc-lines-wrap">
          <table className="doc-lines">
            <thead>
              <tr>
                <th>Part #</th><th>Name</th><th>Details</th><th>Notes</th>
                <th className="num">Qty</th><th>Unit</th>
                <th className="num">Unit price</th><th className="num">Line total</th>
              </tr>
            </thead>
            <tbody>
              {list.map((l, i) => (
                <tr key={i}>
                  <td data-label="Part #">{textOr(l.part_number)}</td>
                  <td data-label="Name">{textOr(l.name)}</td>
                  <td data-label="Details">{textOr(l.details)}</td>
                  <td data-label="Notes">{textOr(l.notes)}</td>
                  <td data-label="Qty" className="num">{qty(l.quantity)}</td>
                  <td data-label="Unit">{textOr(l.unit)}</td>
                  <td data-label="Unit price" className="num">{money(l.unit_price)}</td>
                  <td data-label="Line total" className="num">{money(l.line_total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function Totals({ totals }) {
  const t = isObj(totals) ? totals : {}
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">Totals (as printed)</h3>
      <dl className="admin-fields">
        <Field label="Subtotal" value={money(t.subtotal)} />
        <Field label="Tax" value={money(t.tax)} />
        <Field label="Freight" value={money(t.freight)} />
        <Field label="Total" value={money(t.total)} />
      </dl>
    </div>
  )
}

// Body (exported so it can be rendered with fixture data).
export function DocumentDetailBody({ d, onDelete, deleting, deleteNote }) {
  const r = isObj(d.result) ? d.result : null
  const canDelete = canDeleteDocument(d)
  return (
    <div className="admin-detail">
      <div className="admin-col">
        <Header d={d} />
      </div>
      <div className="admin-col">
        {r && <Checks checks={r.checks} />}
        {r && <Totals totals={r.totals} />}
      </div>
      <div className="admin-col admin-col-full">
        {r && <Lines lines={r.lines} />}
        <div className="admin-card admin-delete">
          <h3 className="admin-card-title">Delete</h3>
          <p className="admin-note">
            Removes this document (the PDF and what was read from it) from this app.
          </p>
          <button className="btn btn-danger" disabled={!canDelete || deleting} onClick={onDelete}>
            {deleting ? 'Deleting…' : 'Delete document'}
          </button>
          {!canDelete && (
            <p className="admin-note">It is still being read. Wait until it is done or failed.</p>
          )}
          {deleteNote && <p className="admin-note is-error">{deleteNote}</p>}
        </div>
      </div>
    </div>
  )
}

// One vendor document: header, checks (warnings first), lines, totals, Delete.
export default function DocumentDetail({ docId, onBack, onDeleted }) {
  const [d, setD] = useState(null)
  const [error, setError] = useState(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteNote, setDeleteNote] = useState(null)
  const [tick, setTick] = useState(0)
  const mounted = useRef(true)

  const load = useCallback(async () => {
    const data = await fetchDocument(docId)
    if (mounted.current) setD(data)
    return data
  }, [docId])

  useEffect(() => {
    mounted.current = true
    setD(null)
    setError(null)
    load().catch((e) => { if (mounted.current) setError((e && e.message) || String(e)) })
    return () => { mounted.current = false }
  }, [load])

  // Still being read: refresh every 5 s (a transient failure just tries again).
  const reading = isReading(d)
  useEffect(() => {
    if (!reading || deleting) return undefined
    const t = setTimeout(() => {
      load().catch((e) => {
        if (!mounted.current) return
        if (e && e.status === 404) setError((e && e.message) || String(e))
        else setTick((n) => n + 1)
      })
    }, REFRESH_MS)
    return () => clearTimeout(t)
  }, [reading, d, deleting, load, tick])

  const onDelete = async () => {
    if (!d) return
    const ok = window.confirm(deleteDocumentPrompt(str(d.filename), str(d.customer_name)))
    if (!ok) return
    setDeleting(true)
    setDeleteNote(null)
    try {
      await deleteDocument(docId)
      if (mounted.current) onDeleted(docId)
    } catch (e) {
      if (!mounted.current) return
      setDeleting(false)
      setDeleteNote((e && e.message) || String(e))
      load().catch(() => { /* keep the current view */ })
    }
  }

  const r = d && isObj(d.result) ? d.result : null
  return (
    <section className="screen screen-admin screen-documents">
      <header className="topbar">
        <button className="topbar-back" onClick={onBack} aria-label="Back">←</button>
        <div className="topbar-text">
          <span className="topbar-eyebrow">Vendor document</span>
          <h2 className="topbar-title">
            {(r && str(r.vendor)) || (d && str(d.filename)) || 'Vendor document'}
          </h2>
        </div>
      </header>
      <div className="admin-body">
        {error && (
          <div className="state error">
            <p><strong>Couldn&rsquo;t load this document.</strong></p>
            <p className="state-detail">{error}</p>
            <button className="btn btn-secondary" onClick={() => {
              setError(null)
              load().catch((e) => setError((e && e.message) || String(e)))
            }}>Retry</button>
          </div>
        )}
        {!error && !d && <div className="state loading">Loading…</div>}
        {!error && d && (
          <DocumentDetailBody d={d} onDelete={onDelete} deleting={deleting} deleteNote={deleteNote} />
        )}
      </div>
    </section>
  )
}
