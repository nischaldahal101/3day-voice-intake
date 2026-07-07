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

const INTAKE_KEY = import.meta.env.VITE_INTAKE_KEY || 'dev-intake-key'

function authHeaders(extra = {}) {
  return { 'x-intake-key': INTAKE_KEY, ...extra }
}

// -------------------- Customers (unchanged) --------------------

export async function fetchCustomers() {
  const resp = await fetch(`${API_BASE}/app/customers`, {
    headers: authHeaders(),
  })
  if (!resp.ok) {
    const detail = await resp.text().catch(() => '')
    throw new Error(
      `Couldn't load customers (HTTP ${resp.status}). ${detail.slice(0, 200)}`,
    )
  }
  const data = await resp.json()
  return data.customers || []
}

export async function createCustomer({ first_name, last_name, phone, email }) {
  const resp = await fetch(`${API_BASE}/app/customers`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      first_name,
      last_name,
      phone,
      email: email && email.trim() ? email.trim() : null,
    }),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    const msg =
      (data && data.error) ||
      `Couldn't create customer (HTTP ${resp.status}).`
    const err = new Error(msg)
    err.status = resp.status
    throw err
  }
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
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      filename,
      content_type: contentType,
      customer_id: customerId || null,
    }),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    const err = new Error(
      (data && data.error) || `upload-url failed (HTTP ${resp.status})`,
    )
    err.status = resp.status
    throw err
  }
  return data // { upload_url, audio_key }
}

// XHR (not fetch) so we can report upload progress. R2 requires the
// Content-Type header to match what was signed byte-for-byte.
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
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      audio_key: audioKey,
      customer_id: customerId || null,
      source: source || '3day-app',
    }),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    const err = new Error(
      (data && data.error) || `submit failed (HTTP ${resp.status})`,
    )
    err.status = resp.status
    throw err
  }
  return data // { job_id, status }
}

export async function fetchJobStatus(jobId) {
  const resp = await fetch(
    `${API_BASE}/job-status/${encodeURIComponent(jobId)}`,
    { headers: authHeaders() },
  )
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    const err = new Error(
      (data && data.error) || `job-status failed (HTTP ${resp.status})`,
    )
    err.status = resp.status
    throw err
  }
  return data
}

export async function fetchRecentJobs(limit = 20) {
  const resp = await fetch(
    `${API_BASE}/jobs/recent?limit=${encodeURIComponent(limit)}`,
    { headers: authHeaders() },
  )
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    throw new Error(
      (data && data.error) || `recent jobs failed (HTTP ${resp.status})`,
    )
  }
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
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ transcript, customer_id: customerId, source }),
  })
  const data = await resp.json().catch(() => ({}))
  return { ok: resp.ok, status: resp.status, data }
}
