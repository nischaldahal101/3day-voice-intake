// Tracks the *currently in-flight* consultation job across page reloads.
// When present, App.jsx picks it up on mount and resumes polling — so a
// nervous rep who force-quits Safari can come right back to the same
// status view.
//
// Once a job reaches a terminal state (done/failed) we clear this key —
// past jobs live in the Recent list (fetched from the backend), not here.

const KEY = 'voiceIntake.activeJob'

export function setActiveJob(active) {
  try {
    localStorage.setItem(KEY, JSON.stringify(active))
  } catch (_) { /* private mode / quota — ignore */ }
}

export function getActiveJob() {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return null
    return JSON.parse(raw)
  } catch (_) {
    return null
  }
}

export function clearActiveJob() {
  try { localStorage.removeItem(KEY) } catch (_) { /* ignore */ }
}

// Forget `jobId` if it is the stored in-flight job (e.g. it was deleted in
// the admin panel), so a reload doesn't try to resume a job that is gone.
export function forgetJob(jobId) {
  const active = getActiveJob()
  if (active && jobId && active.job_id === jobId) clearActiveJob()
}
