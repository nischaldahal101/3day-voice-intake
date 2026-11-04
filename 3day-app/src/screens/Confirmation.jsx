function fmtMoney(v) {
  if (v == null) return null
  const n = Number(v)
  if (!Number.isFinite(n)) return null
  return n.toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  })
}

function capitalize(s) {
  if (!s || typeof s !== 'string') return s
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function highlightsFor(extraction) {
  if (!extraction) return []
  const out = []

  const project = extraction.project_type
  if (project && project !== 'unknown') {
    out.push({ label: 'Project', value: capitalize(project) + ' remodel' })
  }

  const scope = extraction.scope || {}
  if (scope.project_vision) {
    out.push({ label: 'Vision', value: scope.project_vision })
  }

  const sales = extraction.sales || {}
  const lo = fmtMoney(sales.budget_low)
  const hi = fmtMoney(sales.budget_high)
  if (lo && hi) out.push({ label: 'Budget', value: `${lo}–${hi}` })
  else if (sales.budget_raw) out.push({ label: 'Budget', value: sales.budget_raw })

  if (Number.isFinite(sales.close_score_1_10)) {
    out.push({ label: 'Close score', value: `${sales.close_score_1_10} / 10` })
  }
  if (Number.isFinite(sales.likelihood_percent)) {
    out.push({ label: 'Likelihood', value: `${sales.likelihood_percent}%` })
  }

  const ra = extraction.return_appointment
  if (ra && ra.scheduled) {
    out.push({ label: 'Return visit', value: ra.details || 'Scheduled' })
  }

  return out.slice(0, 5)
}

// Plain-language label per step status (backend: fieldpulse_sync.py).
const STEP_LABEL = {
  planned: 'Would do',
  succeeded: 'Done',
  failed: 'Failed',
  sent: 'Sent, no answer',
  skipped: 'Skipped',
  held: 'Not sent (dry run)',
}

const SYNC_TITLE = {
  dry_run: 'FieldPulse: dry run',
  off: 'FieldPulse: off',
  skipped: 'FieldPulse: not linked',
  blocked: 'FieldPulse: not sent',
  succeeded: 'FieldPulse: updated',
  partial: 'FieldPulse: partly updated',
  failed: 'FieldPulse: update failed',
  pending: 'FieldPulse: pending',
}

function str(v) {
  return typeof v === 'string' && v.trim() ? v : null
}

// New jobs carry result.fieldpulse_sync; old mock-writer jobs don't (they
// render exactly as before). Every field is re-guarded: this is server JSON.
function SyncPanel({ sync }) {
  if (!sync || typeof sync !== 'object' || Array.isArray(sync)) return null
  const status = str(sync.status) || 'pending'
  const message = str(sync.message)
  const steps = Array.isArray(sync.steps)
    ? sync.steps.filter((s) => s && typeof s === 'object' && str(s.description))
    : []
  const comment = str(sync.comment_text)
  return (
    <div className={`confirm-sync sync-${status.replace(/[^a-z_]/g, '')}`}>
      <p className="confirm-sync-title">{SYNC_TITLE[status] || 'FieldPulse'}</p>
      {message && <p className="confirm-sync-msg">{message}</p>}
      {steps.length > 0 && (
        <ul className="confirm-sync-steps">
          {steps.map((s, i) => (
            <li key={`${str(s.step) || 'step'}-${i}`}>
              <span className="confirm-sync-step-status">
                {STEP_LABEL[s.status] || str(s.status) || ''}
              </span>
              <span className="confirm-sync-step-text">
                {s.description}
                {str(s.error) && s.status !== 'skipped' ? <> ({s.error})</> : null}
              </span>
            </li>
          ))}
        </ul>
      )}
      {comment && (
        <details className="confirm-transcript">
          <summary className="confirm-transcript-toggle">
            <span>
              {status === 'dry_run' ? 'Comment that would be added' : 'FieldPulse comment'}
            </span>
            <span className="confirm-transcript-count">
              {comment.length.toLocaleString()} chars
            </span>
          </summary>
          <div className="confirm-transcript-body">
            <p className="confirm-transcript-text">{comment}</p>
          </div>
        </details>
      )}
    </div>
  )
}

export default function Confirmation({ customer, result, onDone }) {
  const name =
    (customer && customer.display_name) ||
    (customer &&
      `${customer.first_name || ''} ${customer.last_name || ''}`.trim()) ||
    'the customer'

  const items = highlightsFor(result && result.extraction)
  const fpUrl = result && result.fieldpulse && result.fieldpulse.fieldpulse_url
  const elapsed = result && result.elapsed_seconds
  const matchPath = result && result.fieldpulse && result.fieldpulse.match_path
  const transcript = (result && typeof result.transcript === 'string')
    ? result.transcript
    : ''

  return (
    <section className="screen screen-confirmation">
      <div className="confirm-card">
        <div className="confirm-badge" aria-hidden>
          <svg viewBox="0 0 24 24" width="28" height="28">
            <path
              d="M5 12.5l4.5 4.5L19 7"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>
        <p className="confirm-eyebrow">Consultation saved</p>
        <h2 className="confirm-title">{name}</h2>
        {elapsed != null && (
          <p className="confirm-meta">
            Captured in {Math.round(elapsed)}s
            {matchPath ? <> · {matchPath.replace(/_/g, ' ')}</> : null}
          </p>
        )}
        {items.length > 0 && (
          <ul className="confirm-list">
            {items.map((it) => (
              <li key={it.label}>
                <span className="confirm-label">{it.label}</span>
                <span className="confirm-value">{it.value}</span>
              </li>
            ))}
          </ul>
        )}
        <SyncPanel sync={result && result.fieldpulse_sync} />
        {transcript && (
          <details className="confirm-transcript">
            <summary className="confirm-transcript-toggle">
              <span>Show transcript</span>
              <span className="confirm-transcript-count">
                {transcript.length.toLocaleString()} chars
              </span>
            </summary>
            <div className="confirm-transcript-body">
              <p className="confirm-transcript-text">{transcript}</p>
            </div>
          </details>
        )}
      </div>
      <div className="footer-actions">
        {fpUrl && (
          <a
            className="btn btn-secondary"
            href={fpUrl}
            target="_blank"
            rel="noreferrer"
          >
            View in FieldPulse
          </a>
        )}
        <button className="btn btn-primary btn-lg" onClick={onDone}>
          Done
        </button>
      </div>
    </section>
  )
}
