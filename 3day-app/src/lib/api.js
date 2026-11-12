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

const CUSTOMER_TEXT_FIELDS = [
  'display_name', 'first_name', 'last_name', 'phone', 'email', 'company_name', 'address', 'created_at',
]

// String or null, so a bad field from the server can never reach React as an
// object child. Numbers become strings.
function textOrNull(v) {
  if (typeof v === 'string') return v.trim() || null
  if (typeof v === 'number' && Number.isFinite(v)) return String(v)
  return null
}

function normalizeCustomer(c) {
  const ok = c && typeof c === 'object' &&
    ((typeof c.id === 'string' && c.id.trim()) || (typeof c.id === 'number' && Number.isFinite(c.id)))
  if (!ok) throw new Error("Couldn't load customers: the server sent a customer without a usable id.")
  const out = { id: c.id }
  for (const f of CUSTOMER_TEXT_FIELDS) out[f] = textOrNull(c[f])
  return out
}

// Read-only. Returns customers in the order the server sends them (newest
// added first), each as {id, display_name, first_name, last_name, phone,
// email, company_name, address, created_at} with every non-id field a string
// or null. company_name/address are display-only (stored on the job for the
// admin panel header; never sent to FieldPulse).
// Customers are created in FieldPulse, never from this app.
export async function fetchCustomers() {
  const resp = await fetch(`${API_BASE}/app/customers`, {
    headers: await authHeaders(),
  })
  await assertOk(resp, "Couldn't load customers")
  const data = await resp.json()
  // Don't turn a malformed body into an empty list (review-log D-1).
  if (!data || !Array.isArray(data.customers)) {
    throw new Error("Couldn't load customers: the server sent an unexpected response.")
  }
  return data.customers.map(normalizeCustomer)
}

// The selected customer's identity, sent with submit and stored on the job
// (display only: names in Processing/Confirmation). The FieldPulse write uses
// only the id.
function customerIdentity(customer) {
  if (!customer || customer.id === undefined || customer.id === null) return null
  return {
    id: customer.id,
    display_name: textOrNull(customer.display_name),
    first_name: textOrNull(customer.first_name),
    last_name: textOrNull(customer.last_name),
    phone: textOrNull(customer.phone),
    email: textOrNull(customer.email),
    company_name: textOrNull(customer.company_name), // display only (admin panel header)
    address: textOrNull(customer.address),           // display only (admin panel header)
  }
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

// `customer` is the picker's selected customer object (or null). Send exactly
// one input: `audioKey` (audio already PUT to R2) or `transcript` (pasted
// text; the worker skips transcription). Both go through the same job queue.
export async function submitConsultationJob({ audioKey, transcript, customer, source }) {
  const identity = customerIdentity(customer)
  const input = audioKey ? { audio_key: audioKey } : { transcript }
  const resp = await fetch(`${API_BASE}/consultation/submit`, {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      ...input,
      customer_id: identity ? identity.id : null,
      customer: identity,
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

// -------------------- Admin panel (stage 3) --------------------
// All read from our own database (never FieldPulse). Two actions: delete
// (our database only, see deleteAdminConsultation) and requestAdminSend,
// which just queues a worker job; the worker's write mode decides whether
// anything actually reaches FieldPulse.

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

// { items, total, offset, limit, counts, labels, truncated }
export async function fetchAdminConsultations({ q = '', fp = '', offset = 0, limit = 50 } = {}) {
  const params = new URLSearchParams({ q, fp, offset: String(offset), limit: String(limit) })
  const resp = await fetch(`${API_BASE}/admin/consultations?${params}`, {
    headers: await authHeaders(),
  })
  await assertOk(resp, "Couldn't load consultations")
  const data = await resp.json()
  if (!isPlainObject(data) || !Array.isArray(data.items)) {
    throw new Error("Couldn't load consultations: the server sent an unexpected response.")
  }
  return data
}

export async function fetchAdminConsultation(jobId) {
  const resp = await fetch(`${API_BASE}/admin/consultations/${encodeURIComponent(jobId)}`, {
    headers: await authHeaders(),
  })
  await assertOk(resp, "Couldn't load the consultation")
  const data = await resp.json()
  if (!isPlainObject(data) || typeof data.id !== 'string') {
    throw new Error("Couldn't load the consultation: the server sent an unexpected response.")
  }
  return data
}

// Queue an explicit manual send. Returns { request_id, status, already_queued? }.
export async function requestAdminSend(jobId, requestedBy) {
  const resp = await fetch(`${API_BASE}/admin/consultations/${encodeURIComponent(jobId)}/send`, {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ requested_by: requestedBy || null }),
  })
  await assertOk(resp, 'Send to FieldPulse')
  return resp.json()
}

// Short-lived (15 min) link to the recording. Returns the URL string.
export async function fetchAdminAudioUrl(jobId) {
  const resp = await fetch(
    `${API_BASE}/admin/consultations/${encodeURIComponent(jobId)}/audio-url`,
    { headers: await authHeaders() },
  )
  await assertOk(resp, "Couldn't get the audio link")
  const data = await resp.json()
  if (!data || typeof data.url !== 'string') throw new Error("Couldn't get the audio link.")
  return data.url
}

// Delete a consultation from OUR database (job row + FieldPulse write records)
// and its audio. Never touches FieldPulse. Returns { deleted, id, audio }.
// 409 = still processing / a send is in flight; 404 = already gone.
export async function deleteAdminConsultation(jobId) {
  const resp = await fetch(`${API_BASE}/admin/consultations/${encodeURIComponent(jobId)}`, {
    method: 'DELETE',
    headers: await authHeaders(),
  })
  await assertOk(resp, 'Delete')
  return resp.json()
}

// -------------------- Vendor documents (stage P1b) --------------------
// Vendor PDFs (quotes, order previews, receipts) uploaded for a customer and read
// by the worker into product lines. Our database + R2 only: nothing here (or in
// the backend for these routes) talks to FieldPulse.

export const MAX_PDF_BYTES = 20 * 1024 * 1024
const PDF_TYPE = 'application/pdf'

// Why this file can't be uploaded, in plain words ('' = fine). The worker
// checks the bytes really are a PDF.
export function pdfFileProblem(file) {
  if (!file) return 'Choose a PDF file.'
  if (typeof file.name !== 'string' || !/\.pdf$/i.test(file.name.trim())) {
    return 'That file is not a PDF.'
  }
  if (!(file.size > 0)) return 'That file is empty.'
  if (file.size > MAX_PDF_BYTES) return 'That PDF is larger than 20 MB.'
  return ''
}

// The whole upload: ask for a presigned URL, PUT the PDF straight to R2
// (progress 0..1 via onProgress), then submit it for reading. Returns
// { document_id, status } (status 'queued'). The customer is the picker's
// selected customer (same identity shape as a consultation).
export async function uploadVendorDocument({ file, customer, onProgress }) {
  const problem = pdfFileProblem(file)
  if (problem) throw new Error(problem)
  const identity = customerIdentity(customer)
  if (!identity) throw new Error('Pick a customer first.')
  const filename = file.name.trim()

  let resp = await fetch(`${API_BASE}/documents/upload-url`, {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ filename, size: file.size }),
  })
  await assertOk(resp, 'Upload document')
  const up = await resp.json()
  if (!isPlainObject(up) || typeof up.url !== 'string' || typeof up.key !== 'string') {
    throw new Error("Couldn't prepare the upload: the server sent an unexpected response.")
  }
  // The content type is part of the R2 signature: send exactly what was signed.
  await putToR2(up.url, file, up.content_type || PDF_TYPE, onProgress)

  resp = await fetch(`${API_BASE}/documents/submit`, {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ file_key: up.key, filename, customer: identity }),
  })
  await assertOk(resp, 'Submit document')
  return resp.json()
}

// -------------------- Vendor documents per customer (stage P2) --------------------
// The combined vendor material list is built and sent by the worker (mode-gated);
// these calls only read our database or queue that job.

// { items: [{customer_id, customer_name, document_count, last_upload_at,
//   processing_count, warn_count, fieldpulse_status, fieldpulse_label}], total },
// newest upload first.
export async function fetchDocumentCustomers() {
  const resp = await fetch(`${API_BASE}/documents/customers`, { headers: await authHeaders() })
  await assertOk(resp, "Couldn't load vendor document customers")
  const data = await resp.json()
  if (!isPlainObject(data) || !Array.isArray(data.items)) {
    throw new Error("Couldn't load vendor document customers: the server sent an unexpected response.")
  }
  return data
}

// { customer_id, customer, customer_name, documents: [compact, newest first],
//   processing_count, preview: {title, notes, sections, line_count, document_count},
//   fieldpulse: {status, label, list_id, mode, last_error, updated_at, last_request,
//   last_response, history} }
export async function fetchDocumentCustomer(customerId) {
  const resp = await fetch(`${API_BASE}/documents/customers/${encodeURIComponent(customerId)}`, {
    headers: await authHeaders(),
  })
  await assertOk(resp, "Couldn't load the customer's documents")
  const data = await resp.json()
  if (!isPlainObject(data) || !Array.isArray(data.documents) || !isPlainObject(data.preview)) {
    throw new Error("Couldn't load the customer's documents: the server sent an unexpected response.")
  }
  return data
}

// Queue the manual "Send to FieldPulse" of the customer's combined list (202).
export async function requestVendorListSend(customerId) {
  const resp = await fetch(
    `${API_BASE}/documents/customers/${encodeURIComponent(customerId)}/send`,
    { method: 'POST', headers: await authHeaders() },
  )
  await assertOk(resp, 'Send vendor list')
  return resp.json()
}

// One document: {id, status, error, created_at, customer_name, filename, can_delete,
// result: {vendor, document_type, document_number, document_date, job_name,
// lines, totals, checks} | null}.
export async function fetchDocument(docId) {
  const resp = await fetch(`${API_BASE}/documents/${encodeURIComponent(docId)}`, {
    headers: await authHeaders(),
  })
  await assertOk(resp, "Couldn't load the document")
  const data = await resp.json()
  if (!isPlainObject(data) || typeof data.id !== 'string') {
    throw new Error("Couldn't load the document: the server sent an unexpected response.")
  }
  return data
}

// Delete the document (our database + its PDF in R2). 409 (thrown, server's message)
// while it is still being read. 404 = already gone: treated as deleted
// ({deleted: true, already_gone: true}). On success the server queues the customer's
// vendor list rebuild: materials 'queued' | 'not_queued'.
export async function deleteDocument(docId) {
  const resp = await fetch(`${API_BASE}/documents/${encodeURIComponent(docId)}`, {
    method: 'DELETE',
    headers: await authHeaders(),
  })
  if (resp.status === 404) return { deleted: true, id: docId, already_gone: true }
  await assertOk(resp, 'Delete document')
  return resp.json()
}
