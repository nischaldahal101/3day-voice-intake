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

export async function submitConsultation({ audioBlob, transcript, customerId, source = '3day-app' }) {
  const url = `${API_BASE}/process-consultation`
  let resp
  if (audioBlob) {
    const form = new FormData()
    // If this is a real File (uploaded from disk), preserve its name so the
    // backend can infer the correct MIME from the extension. Recorded blobs
    // have no .name — synthesize one from the MIME type.
    const filename =
      (audioBlob.name && audioBlob.name.trim()) ||
      (audioBlob.type && audioBlob.type.includes('mp4') ? 'consultation.m4a' :
       audioBlob.type && audioBlob.type.includes('ogg') ? 'consultation.ogg' :
       'consultation.webm')
    form.append('audio', audioBlob, filename)
    if (customerId) form.append('customer_id', customerId)
    if (source) form.append('source', source)
    resp = await fetch(url, { method: 'POST', headers: authHeaders(), body: form })
  } else {
    resp = await fetch(url, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ transcript, customer_id: customerId, source }),
    })
  }
  const data = await resp.json().catch(() => ({}))
  // Always return the parsed body — the orchestrator returns 422/502 with a
  // structured status field, and the caller decides how to render it.
  return { ok: resp.ok, status: resp.status, data }
}
