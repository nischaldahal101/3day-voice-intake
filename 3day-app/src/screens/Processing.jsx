// Display-only screen. App.jsx owns the actual upload + polling loop and
// passes down the current phase; this component just renders it.
//
// The point that keeps a rep calm: once submitted, the backend keeps
// processing whether the app is open or not. If they force-quit and come
// back, App.jsx re-attaches to the same job via localStorage.

const PHASES = {
  uploading:  { title: 'Uploading audio…',     sub: 'Sending the recording straight to secure storage.' },
  submitting: { title: 'Queuing consultation…', sub: 'Telling the backend to start.' },
  queued:     { title: 'Queued for processing', sub: 'Waiting for a worker to pick it up (usually a second or two).' },
  processing: { title: 'Transcribing & analyzing…', sub: 'Recordings are transcribed first (pasted text skips this), then Claude pulls the structured details out.' },
}

export default function Processing({ customer, phase, uploadPct, jobId, onLeave }) {
  const name = (customer && customer.display_name) || 'this customer'
  const p = PHASES[phase] || PHASES.processing
  const pctInt = Math.round((uploadPct || 0) * 100)

  return (
    <section className="screen screen-processing">
      <div className="processing-card">
        <div className="processing-spinner" aria-hidden>
          <span /><span /><span />
        </div>
        <h2 className="processing-title">{p.title}</h2>

        {phase === 'uploading' && (
          <div className="upload-progress" aria-live="polite">
            <div className="upload-progress-bar">
              <div
                className="upload-progress-fill"
                style={{ width: `${pctInt}%` }}
              />
            </div>
            <p className="upload-progress-label">{pctInt}%</p>
          </div>
        )}

        <p className="processing-sub">
          {p.sub}
          {customer ? (
            <>
              {' '}Consultation for <strong>{name}</strong>.
            </>
          ) : null}
        </p>
        <p className="processing-meta">
          Long recordings (20+&nbsp;min) can take a couple minutes to process.
        </p>

        <div className="processing-safe">
          <p className="processing-safe-title">You can close the app.</p>
          <p className="processing-safe-sub">
            The consultation keeps processing on our servers. When you come
            back, this screen will pick up right where it left off.
          </p>
        </div>

        {jobId && (
          <p className="processing-jobid" title="Job ID">
            {jobId}
          </p>
        )}

        {onLeave && (
          <button className="btn btn-tertiary" onClick={onLeave}>
            Leave and come back later
          </button>
        )}
      </div>
    </section>
  )
}
