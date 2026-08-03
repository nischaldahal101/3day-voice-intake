// API base URL.
//
//  - Production builds: set VITE_API_BASE_URL (Vite bakes it in at build
//    time). `.env.production` in the app root holds the deployed value.
//  - Local dev: when VITE_API_BASE_URL is unset, falls back to
//    http://<current hostname>:5100. That keeps both laptop (localhost)
//    AND phone-on-LAN (192.168.x.x) dev working without any env vars.
//
// Vite only exposes env vars prefixed VITE_ to the browser, and replaces
// `import.meta.env.VITE_*` references at build time — there is no runtime
// lookup, and nothing un-prefixed leaks out.
export const API_BASE = (() => {
  const fromEnv = import.meta.env.VITE_API_BASE_URL
  if (fromEnv) return fromEnv.replace(/\/$/, '')
  const host = (typeof window !== 'undefined' && window.location.hostname) || 'localhost'
  return `http://${host}:5100`
})()

// ---------------------------------------------------------------------------
// Auth: attach the current Clerk session token as a Bearer to every request.
//
// ClerkProvider (main.jsx) sets `window.Clerk` when it mounts, so this
// module — plain JS, not a React component — can reach it globally. If the
// user isn't signed in yet, or Clerk hasn't finished loading, getToken()
// returns null and we send the request unauthenticated (which the
// orchestrator will 401 — the correct signal).
//
// On 401 anywhere in this file we call signOut() so the app's SignedIn
// wrapper flips to SignedOut and the sign-in card reappears. That way a
// stale/expired token doesn't leave the app stuck in a "logged in but every
// request fails" limbo.
// ---------------------------------------------------------------------------

async function getClerkToken() {
  if (typeof window === 'undefined') return null
  const clerk = window.Clerk
  if (!clerk || !clerk.session) return null
  try {
    return await clerk.session.getToken()
  } catch (_) {
    return null
  }
}

async function authHeaders(extra = {}) {
  const token = await getClerkToken()
  const h = { ...extra }
  if (token) h['Authorization'] = `Bearer ${token}`
  return h
}

async function handleAuthFailure() {
  if (typeof window === 'undefined' || !window.Clerk) return
  try { await window.Clerk.signOut() } catch (_) { /* best effort */ }
}

// Throws a labeled Error on a non-2xx response; also triggers signOut on 401
// so the app bounces back to the sign-in screen instead of quietly failing.
async function assertOk(resp, opName) {
  if (resp.ok) return
  if (resp.status === 401) {
    await handleAuthFailure()
    const err = new Error('Your session expired. Please sign in again.')
    err.status = 401
    throw err
  }
  let detail = ''
  try {
    const data = await resp.clone().json()
    detail = data && data.error ? data.error : ''
  } catch (_) {
    try { detail = await resp.text() } catch (_) { /* ignore */ }
  }
  const err = new Error(
    `${opName} failed (HTTP ${resp.status})${detail ? ': ' + detail.slice(0, 200) : ''}`,
  )
  err.status = resp.status
  throw err
}

// -------------------- Customers --------------------

export async function fetchCustomers() {
  const resp = await fetch(`${API_BASE}/app/customers`, {
    headers: await authHeaders(),
  })
  await assertOk(resp, "Couldn't load customers")
  const data = await resp.json()
  return data.customers || []
}

export async function createCustomer({ first_name, last_name, phone, email }) {
  const resp = await fetch(`${API_BASE}/app/customers`, {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      first_name,
      last_name,
      phone,
      email: email && email.trim() ? email.trim() : null,
    }),
  })
  await assertOk(resp, "Couldn't create customer")
  const data = await resp.json().catch(() => ({}))
  return data.customer
}

// -------------------- Async consultation flow --------------------

// The MediaRecorder blob's `.type` often includes codec params, e.g.
// "audio/webm;codecs=opus". R2 signs the exact Content-Type string, so
// what we pass to /consultation/upload-url and what we send in the PUT
// header must match. Strip codec params consistently.
export function baseContentType(ct) {
  return (ct || '').split(';')[0].trim() || 'application/octet-stream'
}

// Pick a sensible filename for a recorded blob (files have their own name).
export function filenameForBlob(blob, fallbackExt) {
  if (blob && blob.name && String(blob.name).trim()) return blob.name
  const ct = baseContentType(blob && blob.type)
  const ext =
    ct === 'audio/webm' ? 'webm' :
    ct === 'video/webm' ? 'webm' :
    ct === 'audio/mp4' ? 'm4a' :
    ct === 'audio/ogg' ? 'ogg' :
    ct === 'audio/wav' ? 'wav' :
    ct === 'audio/mpeg' ? 'mp3' :
    (fallbackExt || 'bin')
  return `consultation.${ext}`
}

export async function requestUploadUrl({ filename, contentType, customerId }) {
  const resp = await fetch(`${API_BASE}/consultation/upload-url`, {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      filename,
      content_type: contentType,
      customer_id: customerId || null,
    }),
  })
  await assertOk(resp, 'upload-url')
  return resp.json() // { upload_url, audio_key }
}

// XHR (not fetch) so we can report upload progress. R2 requires the
// Content-Type header to match what was signed byte-for-byte.
// Note: this hits R2 directly (presigned URL), NOT our orchestrator — no
// Clerk token attaches or should attach.
export function putToR2(uploadUrl, blob, contentType, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('PUT', uploadUrl, true)
    xhr.setRequestHeader('Content-Type', contentType)
    if (onProgress && xhr.upload) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          onProgress(e.loaded / e.total)
        }
      }
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve()
      else reject(new Error(`R2 upload failed (HTTP ${xhr.status}): ${xhr.responseText.slice(0, 200)}`))
    }
    xhr.onerror = () => reject(new Error('Network error uploading to R2'))
    xhr.ontimeout = () => reject(new Error('Timed out uploading to R2'))
    xhr.send(blob)
  })
}

export async function submitConsultationJob({ audioKey, customerId, source }) {
  const resp = await fetch(`${API_BASE}/consultation/submit`, {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      audio_key: audioKey,
      customer_id: customerId || null,
      source: source || '3day-app',
    }),
  })
  await assertOk(resp, 'submit')
  return resp.json() // { job_id, status }
}

export async function fetchJobStatus(jobId) {
  const resp = await fetch(
    `${API_BASE}/job-status/${encodeURIComponent(jobId)}`,
    { headers: await authHeaders() },
  )
  await assertOk(resp, 'job-status')
  return resp.json()
}

export async function fetchRecentJobs(limit = 20) {
  const resp = await fetch(
    `${API_BASE}/jobs/recent?limit=${encodeURIComponent(limit)}`,
    { headers: await authHeaders() },
  )
  await assertOk(resp, 'recent jobs')
  const data = await resp.json()
  return data.jobs || []
}

// -------------------- Sync fallback (paste/type only) --------------------
// Used only by the Paste tab. The old sync endpoint on the orchestrator is
// marked deprecated but kept working — it's the only path that accepts a
// pre-transcribed transcript, and the paste fallback exists specifically
// for iOS Safari over HTTP where mic access is blocked.

export async function submitConsultationSync({ transcript, customerId, source = '3day-app' }) {
  const resp = await fetch(`${API_BASE}/process-consultation`, {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ transcript, customer_id: customerId, source }),
  })
  // Preserve the old caller contract: return { ok, status, data } rather
  // than throwing on non-2xx (the Paste flow surfaces friendly errors).
  // But still trigger sign-out on 401 so the app doesn't hang signed-in
  // with a dead token.
  if (resp.status === 401) await handleAuthFailure()
  const data = await resp.json().catch(() => ({}))
  return { ok: resp.ok, status: resp.status, data }
}
