import { useCallback, useEffect, useRef, useState } from 'react'
import { useUser } from '@clerk/clerk-react'
import {
  deleteAdminConsultation, fetchAdminAudioUrl, fetchAdminConsultation, requestAdminSend,
} from '../lib/api.js'
import {
  FP_LABEL, JOB_STATUS_LABEL, MODE_LABEL, SOURCE_LABEL, STEP_NAME, WRITE_STATUS_LABEL,
  copyText, display, fieldPulsePlain, fmtDateTime, fmtEpoch, groupExtraction, isObj,
  keyResults, pretty, str,
} from '../lib/admin.js'

const POLL_MS = 3000
const POLL_TRIES = 40 // ~2 minutes

function cssToken(s) {
  return String(s || '').replace(/[^a-z_]/g, '')
}

function CopyButton({ text, label = 'Copy' }) {
  const [state, setState] = useState(null)
  if (!str(text)) return null
  return (
    <button
      className="btn btn-tertiary admin-copy"
      onClick={async () => {
        const ok = await copyText(text)
        setState(ok ? 'Copied' : 'Copy failed: select the text by hand')
        setTimeout(() => setState(null), 2500)
      }}
    >
      {state || label}
    </button>
  )
}

function RawJson({ value, label = 'Raw JSON' }) {
  if (value === undefined) return null
  return (
    <details className="admin-raw">
      <summary>{label}</summary>
      <pre>{pretty(value)}</pre>
    </details>
  )
}

function Field({ label, children }) {
  return (
    <div className="admin-field">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}

// ---- 1. Summary (what Mike reads first) -----------------------------------

const NOT_MENTIONED = <em className="muted">Not mentioned</em>

function Summary({ d }) {
  const c = isObj(d.customer) ? d.customer : {}
  const phone = str(c.phone)
  const email = str(c.email)
  const fp = fieldPulsePlain(d)
  const sync = isObj(d.fieldpulse_sync) ? d.fieldpulse_sync : null
  return (
    <div className="admin-card admin-summary">
      <h3 className="admin-card-title">Customer</h3>
      <dl className="admin-fields">
        <Field label="Name">{str(d.customer_name) || <em className="muted">No customer linked</em>}</Field>
        <Field label="Phone">{phone ? <a href={`tel:${phone}`}>{phone}</a> : <em className="muted">Not on file</em>}</Field>
        {email && <Field label="Email"><a href={`mailto:${email}`}>{email}</a></Field>}
        <Field label="Address">{str(c.address) || <em className="muted">Not on file</em>}</Field>
        <Field label="Date">{fmtDateTime(d.created_at) || '—'}</Field>
        <Field label="Status">
          <span className={`pill job-status status-${cssToken(d.status)}`}>
            {JOB_STATUS_LABEL[d.status] || str(d.status) || '?'}
          </span>
        </Field>
      </dl>
      <p className={`admin-fp-line fp-line-${fp.kind}`}>
        <strong>FieldPulse: {fp.text}</strong>
        {fp.detail ? <span className="admin-sub"> · {fp.detail}</span> : null}
      </p>
      {sync && str(sync.message) && <p className="admin-note">{sync.message}</p>}
      {fp.kind === 'attention' && (
        <p className="admin-note">Open &ldquo;Details for troubleshooting&rdquo; below to see each step or retry.</p>
      )}
      {str(d.error) && (
        <div className="admin-alert">
          <strong>Error:</strong> {d.error}
        </div>
      )}
    </div>
  )
}

function KeyResults({ extraction }) {
  const k = keyResults(extraction)
  return (
    <div className="admin-card admin-results">
      <h3 className="admin-card-title">Consultation results</h3>
      {!k ? (
        <p className="admin-note">No extracted details (the job did not finish extraction).</p>
      ) : (
        <>
          <div className="admin-stats">
            {k.stats.map((s) => (
              <div className="admin-stat" key={s.key}>
                <span className="admin-stat-label">{s.label}</span>
                <span className={`admin-stat-value${s.value ? '' : ' is-missing'}`}>
                  {s.value || 'Not mentioned'}
                </span>
              </div>
            ))}
          </div>
          <dl className="admin-fields admin-fields-stack">
            {k.rows.map((r) => (
              <Field key={r.key} label={r.label}>
                {r.value ? <span className="admin-pre">{r.value}</span> : NOT_MENTIONED}
              </Field>
            ))}
          </dl>
        </>
      )}
    </div>
  )
}

// Record-level ids for troubleshooting (moved from the old header card).
function RecordInfo({ d }) {
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">Record</h3>
      <dl className="admin-fields">
        <Field label="FieldPulse customer id">{display(d.customer_id) || <em className="muted">None</em>}</Field>
        <Field label="Source">{SOURCE_LABEL[d.source] || str(d.source) || '—'}</Field>
        <Field label="Job status">
          <span className={`pill job-status status-${cssToken(d.status)}`}>{str(d.status) || '?'}</span>
        </Field>
        <Field label="Last update">{fmtDateTime(d.updated_at) || '—'}</Field>
        <Field label="Consultation id"><code>{d.id}</code></Field>
      </dl>
    </div>
  )
}

// ---- 2. FieldPulse (troubleshooting) -----------------------------------------------------------

function TaskBody({ body }) {
  if (!isObj(body)) return null
  return (
    <dl className="admin-fields admin-fields-tight">
      <Field label="Type">{display(body.type) || '—'} · {display(body.job_type) || '—'}</Field>
      <Field label="Title">{display(body.subtitle) || '—'}</Field>
      <Field label="Starts">{fmtEpoch(body.start_time) || display(body.start_time) || '—'}</Field>
      <Field label="Ends">{fmtEpoch(body.end_time) || display(body.end_time) || '—'}</Field>
      <Field label="Customer id">{display(body.customer_id) || '—'}</Field>
      <Field label="Notes"><span className="admin-pre">{display(body.notes) || '—'}</span></Field>
    </dl>
  )
}

function MaterialBody({ body }) {
  if (!isObj(body)) return null
  const groups = Array.isArray(body.groupings) ? body.groupings.filter(isObj) : []
  return (
    <dl className="admin-fields admin-fields-tight">
      <Field label="Title">{display(body.title) || '—'}</Field>
      <Field label="Customer id">{display(body.customer_id) || '—'}</Field>
      <Field label="Notes"><span className="admin-pre">{display(body.notes) || '—'}</span></Field>
      {groups.map((g, i) => (
        <Field key={i} label={display(g.title) || 'Group'}>
          <ul className="admin-list">
            {(Array.isArray(g.line_items) ? g.line_items.filter(isObj) : []).map((li, j) => (
              <li key={j}>
                {display(li.quantity) || '?'}{str(li.unit_measure) ? ` ${li.unit_measure}` : ''} ×{' '}
                {display(li.title) || '—'}
                {str(li.description) ? <span className="admin-sub"> · {li.description}</span> : null}
              </li>
            ))}
          </ul>
        </Field>
      ))}
    </dl>
  )
}

function WriteStep({ w, held }) {
  const req = isObj(w.request) ? w.request : null
  const body = req && isObj(req.body) ? req.body : null
  const status = held && w.status === 'planned' ? 'held' : w.status
  const text = body && str(body.text)
  return (
    <div className={`admin-step step-${cssToken(status)}`}>
      <div className="admin-step-head">
        <strong>{STEP_NAME[w.step] || display(w.step)}</strong>
        <span className={`pill fp-pill wstatus-${cssToken(status)}`}>
          {WRITE_STATUS_LABEL[status] || display(status)}
        </span>
      </div>
      <dl className="admin-fields admin-fields-tight">
        <Field label="Mode">{MODE_LABEL[w.mode] || display(w.mode) || '—'}</Field>
        <Field label="Recorded">{fmtDateTime(w.created_at) || '—'}</Field>
        <Field label="Last update">{fmtDateTime(w.updated_at) || '—'}</Field>
        <Field label="Attempts">{display(w.attempts) || '0'}</Field>
        {str(w.fieldpulse_record_id) && <Field label="FieldPulse record id">{w.fieldpulse_record_id}</Field>}
        {str(w.error) && <Field label={w.status === 'skipped' ? 'Why' : 'Error'}>{w.error}</Field>}
        {req && <Field label="Request">{display(req.method)} {display(req.path)}</Field>}
      </dl>
      {w.status === 'sent' && (
        <p className="admin-warn">
          The request went out but FieldPulse never answered. It may or may not be there.
          Check FieldPulse by hand. It will not be sent again automatically or from here.
        </p>
      )}
      {text && (
        <div className="admin-comment">
          <div className="admin-comment-head">
            <span>Comment text</span>
            <CopyButton text={text} label="Copy comment text" />
          </div>
          <pre className="admin-comment-text">{text}</pre>
        </div>
      )}
      {w.step === 'return_task' && body && <TaskBody body={body} />}
      {w.step === 'material_list' && body && <MaterialBody body={body} />}
      <RawJson value={w.request} label="Request (raw JSON)" />
      {w.response != null && <RawJson value={w.response} label="FieldPulse response (raw JSON)" />}
    </div>
  )
}

function SendControls({ d, onSend, sending, sendNote }) {
  const fp = d.fieldpulse_status
  const retry = fp === 'failed' || fp === 'partial'
  return (
    <div className="admin-send">
      <button
        className="btn btn-primary"
        disabled={!d.can_send || sending}
        onClick={onSend}
      >
        {sending ? 'Sending…' : retry ? 'Retry FieldPulse send' : 'Send to FieldPulse'}
      </button>
      {!d.can_send && (
        <p className="admin-note">Only a finished consultation with extracted details can be sent.</p>
      )}
      {sendNote && <p className={`admin-note ${sendNote.kind === 'error' ? 'is-error' : ''}`}>{sendNote.text}</p>}
    </div>
  )
}

function ManualSends({ sends: raw }) {
  const sends = Array.isArray(raw) ? raw.filter(isObj) : []
  if (sends.length === 0) return null
  return (
    <details className="admin-raw">
      <summary>Manual send history ({sends.length})</summary>
      <ul className="admin-history">
        {sends.slice().reverse().map((s, i) => (
          <li key={str(s.id) || i}>
            <strong>{fmtDateTime(s.requested_at) || '—'}</strong>
            {str(s.requested_by) ? ` by ${s.requested_by}` : ''}: {display(s.status) || '?'}
            {str(s.outcome) ? ` (${s.outcome})` : ''}
            {str(s.message) ? <> · {s.message}</> : null}
          </li>
        ))}
      </ul>
    </details>
  )
}

function FieldPulseSection({ d, onSend, sending, sendNote }) {
  const sync = isObj(d.fieldpulse_sync) ? d.fieldpulse_sync : null
  const writes = Array.isArray(d.fieldpulse_writes) ? d.fieldpulse_writes.filter(isObj) : []
  const heldSteps = new Set(
    sync && Array.isArray(sync.steps)
      ? sync.steps.filter((s) => isObj(s) && s.status === 'held').map((s) => s.step)
      : [],
  )
  const hasCommentRow = writes.some((w) => w.step === 'comment' && isObj(w.request))
  const summaryComment = sync && str(sync.comment_text)
  const legacy = isObj(d.legacy_fieldpulse) ? d.legacy_fieldpulse : null
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">
        FieldPulse
        <span className={`pill fp-pill fp-${cssToken(d.fieldpulse_status)}`}>
          {str(d.fieldpulse_label) || FP_LABEL[d.fieldpulse_status] || '—'}
        </span>
      </h3>
      {sync && str(sync.message) && <p className="admin-msg">{sync.message}</p>}
      {sync && str(sync.task_skipped_reason) && (
        <p className="admin-note">No return-appointment task: {sync.task_skipped_reason}</p>
      )}
      {sync && str(sync.synced_at) && (
        <p className="admin-note">
          Last sync: {fmtDateTime(sync.synced_at)}
          {str(sync.mode) ? ` · mode ${MODE_LABEL[sync.mode] || sync.mode}` : ''}
          {sync.manual ? ' · manual send' : ''}
        </p>
      )}
      <SendControls d={d} onSend={onSend} sending={sending} sendNote={sendNote} />
      {writes.length === 0 && !legacy && (
        <p className="admin-note">No FieldPulse steps were recorded for this consultation.</p>
      )}
      {writes.map((w, i) => (
        <WriteStep key={`${display(w.step)}-${i}`} w={w} held={heldSteps.has(w.step)} />
      ))}
      {!hasCommentRow && summaryComment && (
        <div className="admin-comment">
          <div className="admin-comment-head">
            <span>Comment text (from the sync summary)</span>
            <CopyButton text={summaryComment} label="Copy comment text" />
          </div>
          <pre className="admin-comment-text">{summaryComment}</pre>
        </div>
      )}
      {legacy && (
        <div className="admin-step">
          <p className="admin-note">
            Old mock-era consultation: it was written to the mock FieldPulse only, never to
            real FieldPulse. The mock&rsquo;s record is below.
          </p>
          <RawJson value={legacy} label="Mock FieldPulse record (raw JSON)" />
        </div>
      )}
      <ManualSends sends={d.manual_sends} />
      {sync && <RawJson value={sync} label="Sync summary (raw JSON)" />}
    </div>
  )
}

// ---- 3. Extracted details (troubleshooting: every field + AI flags) ---------------------------------------------------------

function ExtractionSection({ extraction, taskReason }) {
  if (!isObj(extraction)) {
    return (
      <div className="admin-card">
        <h3 className="admin-card-title">Extracted details</h3>
        <p className="admin-note">No extracted details (the job did not finish extraction).</p>
      </div>
    )
  }
  const { groups, quotes, flags } = groupExtraction(extraction)
  return (
    <div className="admin-card">
      <h3 className="admin-card-title">Extracted details</h3>
      <div className="admin-groups">
        {groups.map((g) => (
          <div className="admin-group" key={g.id}>
            <h4>{g.title}</h4>
            {g.note && <p className="admin-note">{g.note}</p>}
            <dl className="admin-fields">
              {g.rows.map((r) => (
                <Field key={r.key} label={r.label}>
                  {r.missing ? (
                    <span className="admin-missing">
                      Not mentioned{r.unknownAnswer ? ' (unknown)' : ''}
                      {r.flags.map((f, i) => (
                        <span className="admin-flag" key={i}>AI note: {f}</span>
                      ))}
                    </span>
                  ) : r.raw ? (
                    <pre className="admin-pre">{r.value}</pre>
                  ) : (
                    <span className="admin-pre">{r.value}</span>
                  )}
                </Field>
              ))}
              {g.id === 'return' && str(taskReason) && (
                <Field label="FieldPulse task">Not created: {taskReason}</Field>
              )}
            </dl>
          </div>
        ))}
        <div className="admin-group">
          <h4>Quotes</h4>
          {quotes.length ? (
            <ul className="admin-list">{quotes.map((q, i) => <li key={i}>&ldquo;{q}&rdquo;</li>)}</ul>
          ) : <p className="admin-note">None captured.</p>}
        </div>
        <div className="admin-group">
          <h4>AI flags (what was not covered or was inferred)</h4>
          {flags.length ? (
            <ul className="admin-list">{flags.map((f, i) => <li key={i}>{f}</li>)}</ul>
          ) : <p className="admin-note">None.</p>}
        </div>
      </div>
      <RawJson value={extraction} label="Extraction (raw JSON)" />
    </div>
  )
}

// ---- 4. Transcript ----------------------------------------------------------------

function TranscriptSection({ d }) {
  const [audio, setAudio] = useState(null) // {url} | {error} | 'loading'
  const text = str(d.transcript)
  const getAudio = async () => {
    setAudio('loading')
    try {
      setAudio({ url: await fetchAdminAudioUrl(d.id) })
    } catch (e) {
      setAudio({ error: (e && e.message) || String(e) })
    }
  }
  return (
    <details className="admin-card admin-fold">
      <summary className="admin-card-title">
        Transcript and recording
        {text && <span className="admin-sub">{text.length.toLocaleString()} characters</span>}
      </summary>
      <div className="admin-transcript-actions">
        <CopyButton text={text} label="Copy transcript" />
        {d.has_audio && audio !== 'loading' && !(audio && audio.url) && (
          <button className="btn btn-tertiary" onClick={getAudio}>Play / download audio</button>
        )}
        {audio === 'loading' && <span className="admin-note">Getting a link…</span>}
      </div>
      {audio && audio.url && (
        <div className="admin-audio">
          <audio controls preload="none" src={audio.url} />
          <a href={audio.url} target="_blank" rel="noreferrer">Download audio</a>
          <span className="admin-note">Link works for 15 minutes.</span>
        </div>
      )}
      {audio && audio.error && <p className="admin-note is-error">{audio.error}</p>}
      {text
        ? <pre className="admin-transcript">{text}</pre>
        : <p className="admin-note">No transcript saved.</p>}
    </details>
  )
}

// ---- Delete (our database only; FieldPulse is never touched) ----------------------

const BUSY = new Set(['queued', 'processing'])

function DeleteSection({ d, onDelete, deleting, deleteNote, sending }) {
  const busy = BUSY.has(d.status)
  return (
    <div className="admin-card admin-delete">
      <h3 className="admin-card-title">Delete</h3>
      <p className="admin-note">
        Removes this consultation from this app only (transcript, extracted details, FieldPulse
        step records, audio). FieldPulse is not changed: anything already sent there stays.
      </p>
      <button
        className="btn btn-danger"
        disabled={busy || deleting || sending}
        onClick={onDelete}
      >
        {deleting ? 'Deleting…' : 'Delete consultation'}
      </button>
      {busy && (
        <p className="admin-note">It is still being processed. Wait until it is done or failed.</p>
      )}
      {deleteNote && <p className="admin-note is-error">{deleteNote}</p>}
    </div>
  )
}

// ---- Body (exported so it can be rendered with fixture data) ----------------------
// Top: a clean summary for Mike. Below: transcript/recording (folded) and
// "Details for troubleshooting" (folded): every FieldPulse step with the exact
// request/response, Send/Retry, all extracted fields with AI flags, raw row, Delete.

export function AdminDetailBody({ d, onSend, sending, sendNote, onDelete, deleting, deleteNote }) {
  return (
    <div className="admin-detail">
      <div className="admin-col">
        <Summary d={d} />
      </div>
      <div className="admin-col">
        <KeyResults extraction={d.extraction} />
      </div>
      <div className="admin-col admin-col-full">
        <TranscriptSection d={d} />
        <details className="admin-trouble">
          <summary>Details for troubleshooting</summary>
          <div className="admin-detail">
            <div className="admin-col">
              <RecordInfo d={d} />
              <FieldPulseSection d={d} onSend={onSend} sending={sending} sendNote={sendNote} />
            </div>
            <div className="admin-col">
              <ExtractionSection
                extraction={d.extraction}
                taskReason={isObj(d.fieldpulse_sync) ? d.fieldpulse_sync.task_skipped_reason : null}
              />
            </div>
            <div className="admin-col admin-col-full">
              <div className="admin-card">
                <h3 className="admin-card-title">Raw record</h3>
                <p className="admin-note">Everything stored for this consultation, as a last resort.</p>
                <RawJson value={d.raw} label="Whole job row (raw JSON)" />
              </div>
              {onDelete && (
                <DeleteSection d={d} onDelete={onDelete} deleting={deleting}
                  deleteNote={deleteNote} sending={sending} />
              )}
            </div>
          </div>
        </details>
      </div>
    </div>
  )
}

// ---- Screen -----------------------------------------------------------------------

export default function AdminDetail({ jobId, onBack, onDeleted }) {
  const { user } = useUser()
  const [d, setD] = useState(null)
  const [error, setError] = useState(null)
  const [sending, setSending] = useState(false)
  const [sendNote, setSendNote] = useState(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteNote, setDeleteNote] = useState(null)
  const pollRef = useRef(null)
  const mounted = useRef(true)

  const load = useCallback(async () => {
    const data = await fetchAdminConsultation(jobId)
    if (mounted.current) setD(data)
    return data
  }, [jobId])

  useEffect(() => {
    mounted.current = true
    setD(null)
    setError(null)
    setSendNote(null)
    load().catch((e) => { if (mounted.current) setError((e && e.message) || String(e)) })
    return () => {
      mounted.current = false
      clearTimeout(pollRef.current)
    }
  }, [load])

  // Still queued/processing: refresh every 5 s until it finishes (this is what
  // opening an in-progress job from the old Recent list used to do).
  // A transient failure bumps `retryTick` so the next attempt is scheduled
  // (otherwise nothing in the deps changes and refreshing would stop).
  const busy = !!d && BUSY.has(d.status)
  const [retryTick, setRetryTick] = useState(0)
  useEffect(() => {
    if (!busy || sending || deleting) return undefined
    const t = setTimeout(() => {
      load().catch((e) => {
        if (!mounted.current) return
        if (e && e.status === 404) setError((e && e.message) || String(e))
        else setRetryTick((n) => n + 1)
      })
    }, 5000)
    return () => clearTimeout(t)
  }, [busy, d, sending, deleting, load, retryTick])

  const waitFor = useCallback((requestId, tries) => {
    pollRef.current = setTimeout(async () => {
      let data = null
      try { data = await load() } catch (_) { /* transient: keep polling */ }
      if (!mounted.current) return
      const entry = data && Array.isArray(data.manual_sends)
        ? data.manual_sends.find((s) => isObj(s) && s.id === requestId) : null
      if (entry && entry.status !== 'queued') {
        setSending(false)
        setSendNote({
          kind: entry.status === 'error' ? 'error' : 'ok',
          text: str(entry.message) || 'Done.',
        })
        return
      }
      if (tries <= 1) {
        setSending(false)
        setSendNote({
          kind: 'ok',
          text: 'Still waiting for the worker. It is queued; refresh this page in a minute.',
        })
        return
      }
      waitFor(requestId, tries - 1)
    }, POLL_MS)
  }, [load])

  const onSend = async () => {
    const ok = window.confirm(
      'Send this consultation to FieldPulse?\n\n' +
      'The worker\'s write mode decides: in dry run nothing is sent, and in test-customer ' +
      'mode only the test customer is sent. Steps that already succeeded are never sent twice.',
    )
    if (!ok) return
    setSending(true)
    setSendNote({ kind: 'ok', text: 'Queued. Waiting for the worker…' })
    try {
      const name = (user && (user.fullName || (user.primaryEmailAddress &&
        user.primaryEmailAddress.emailAddress))) || null
      const r = await requestAdminSend(jobId, name)
      waitFor(r.request_id, POLL_TRIES)
    } catch (e) {
      setSending(false)
      setSendNote({ kind: 'error', text: (e && e.message) || String(e) })
    }
  }

  const onDelete = async () => {
    if (!d) return
    const ok = window.confirm(
      'Delete this consultation?\n\n' +
      `Customer: ${str(d.customer_name) || 'No customer linked'}\n` +
      `Date: ${fmtDateTime(d.created_at) || 'unknown'}\n\n` +
      'This permanently removes it from this app: transcript, extracted details, FieldPulse ' +
      'step records and the audio recording. It cannot be undone.\n\n' +
      'FieldPulse is NOT changed. Anything already sent to FieldPulse (comment, task) stays there.',
    )
    if (!ok) return
    setDeleting(true)
    setDeleteNote(null)
    try {
      await deleteAdminConsultation(jobId)
      if (mounted.current) onDeleted(jobId)
    } catch (e) {
      if (!mounted.current) return
      setDeleting(false)
      if (e && e.status === 404) { onDeleted(jobId); return } // already gone
      setDeleteNote((e && e.message) || String(e))
      load().catch(() => { /* keep the current view */ })      // e.g. 409: show the new status
    }
  }

  return (
    <section className="screen screen-admin">
      <header className="topbar">
        <button className="topbar-back" onClick={onBack} aria-label="Back">←</button>
        <div className="topbar-text">
          <span className="topbar-eyebrow">Consultation</span>
          <h2 className="topbar-title">{(d && str(d.customer_name)) || 'Consultation'}</h2>
        </div>
      </header>
      <div className="admin-body">
        {error && (
          <div className="state error">
            <p><strong>Couldn&rsquo;t load this consultation.</strong></p>
            <p className="state-detail">{error}</p>
            <button className="btn btn-secondary" onClick={() => {
              setError(null)
              load().catch((e) => setError((e && e.message) || String(e)))
            }}>Retry</button>
          </div>
        )}
        {!error && !d && <div className="state loading">Loading…</div>}
        {!error && d && (
          <AdminDetailBody d={d} onSend={onSend} sending={sending} sendNote={sendNote}
            onDelete={onDeleted ? onDelete : null} deleting={deleting} deleteNote={deleteNote} />
        )}
      </div>
    </section>
  )
}
