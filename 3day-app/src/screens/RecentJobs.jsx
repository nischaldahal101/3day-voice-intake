import { useEffect, useState } from 'react'
import { fetchRecentJobs } from '../lib/api.js'

function timeAgo(iso) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (!Number.isFinite(then)) return ''
  const s = Math.floor((Date.now() - then) / 1000)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function statusLabel(s) {
  return (s || '').replace(/_/g, ' ')
}

function capitalize(s) {
  if (!s || typeof s !== 'string') return s
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function jobTitle(job) {
  const s = job.summary || {}
  if (s.project_type && s.project_type !== 'unknown') {
    return `${capitalize(s.project_type)} consultation`
  }
  if (job.status === 'done') return 'Consultation'
  if (job.status === 'failed') return 'Failed consultation'
  return 'Consultation in progress'
}

const SYNC_SHORT = {
  dry_run: 'FieldPulse: dry run, nothing sent',
  off: 'FieldPulse: off',
  skipped: 'FieldPulse: not linked',
  blocked: 'FieldPulse: not sent',
  succeeded: 'FieldPulse: updated',
  partial: 'FieldPulse: partly updated',
  failed: 'FieldPulse: update failed',
  pending: 'FieldPulse: pending',
}

function customerLabel(job) {
  const c = job.customer
  if (!c || typeof c !== 'object') return null
  const name = typeof c.display_name === 'string' && c.display_name.trim()
  if (name) return name
  const parts = [c.first_name, c.last_name].filter((p) => typeof p === 'string' && p.trim())
  return parts.join(' ') || null
}

function jobSubline(job) {
  const s = job.summary || {}
  const who = customerLabel(job)
  const sync = s.fieldpulse_sync && typeof s.fieldpulse_sync === 'object' ? s.fieldpulse_sync : null
  if (job.status === 'done' && sync) {
    const bits = []
    if (who) bits.push(who)
    if (s.input === 'paste') bits.push('pasted')
    bits.push(SYNC_SHORT[sync.status] || 'FieldPulse')
    return bits.join(' · ')
  }
  if (job.status === 'done') {
    // Old mock-writer jobs (before the real FieldPulse sync).
    const bits = []
    if (who) bits.push(who)
    if (s.match_path) bits.push(statusLabel(s.match_path))
    if (s.fieldpulse_customer_id != null && typeof s.fieldpulse_customer_id !== 'object') {
      bits.push(String(s.fieldpulse_customer_id))
    }
    return bits.join(' · ') || 'Saved to FieldPulse'
  }
  if (job.status === 'failed') return job.error || 'Something went wrong.'
  return who ? `${who} · waiting on the backend worker…` : 'Waiting on the backend worker…'
}

export default function RecentJobs({ onBack, onOpen }) {
  const [jobs, setJobs] = useState(null) // null = loading
  const [error, setError] = useState(null)

  const load = () => {
    setJobs(null)
    setError(null)
    fetchRecentJobs(30)
      .then(setJobs)
      .catch((e) => setError((e && e.message) || String(e)))
  }

  useEffect(() => {
    let cancelled = false
    fetchRecentJobs(30)
      .then((data) => { if (!cancelled) setJobs(data) })
      .catch((e) => { if (!cancelled) setError((e && e.message) || String(e)) })
    return () => { cancelled = true }
  }, [])

  return (
    <section className="screen screen-recent">
      <header className="topbar">
        <button className="topbar-back" onClick={onBack} aria-label="Back">←</button>
        <div className="topbar-text">
          <span className="topbar-eyebrow">Consultations</span>
          <h2 className="topbar-title">Recent</h2>
        </div>
      </header>

      <div className="list-wrap">
        {jobs === null && !error && (
          <div className="state loading">Loading…</div>
        )}
        {error && (
          <div className="state error">
            <p><strong>Couldn&rsquo;t load recent consultations.</strong></p>
            <p className="state-detail">{error}</p>
            <button className="btn btn-secondary" onClick={load}>Retry</button>
          </div>
        )}
        {jobs && jobs.length === 0 && !error && (
          <div className="state empty">
            <p className="empty-msg">No consultations yet.</p>
          </div>
        )}
        {jobs && jobs.length > 0 && (
          <ul className="job-list">
            {jobs.map((j) => (
              <li key={j.id}>
                <button className="job-row" onClick={() => onOpen(j)}>
                  <div className="job-row-top">
                    <span className={`pill job-status status-${j.status}`}>
                      {statusLabel(j.status)}
                    </span>
                    <span className="job-row-time">{timeAgo(j.created_at)}</span>
                  </div>
                  <p className="job-row-title">{jobTitle(j)}</p>
                  <p className="job-row-sub">{jobSubline(j)}</p>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}
