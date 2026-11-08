import { useState } from 'react'
import { MAX_PDF_BYTES, pdfFileProblem, uploadVendorDocument } from '../lib/api.js'
import CustomerProfileCard from './CustomerProfileCard.jsx'

function sizeLabel(bytes) {
  if (!(bytes > 0)) return ''
  return bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1024))} KB`
}

// The customer (picked, or the customer page's) + one vendor PDF (<= 20 MB).
// Uploads it straight to R2, submits it, then hands the new document id back
// (the list / customer page shows it as being read).
export default function DocumentUpload({ customer, step = 'Step 2 of 2', onBack, onUploaded }) {
  const [file, setFile] = useState(null)
  const [problem, setProblem] = useState('')
  const [busy, setBusy] = useState(false)
  const [pct, setPct] = useState(0)
  const [error, setError] = useState(null)

  const choose = (e) => {
    const f = e.target.files && e.target.files[0]
    setFile(f || null)
    setProblem(f ? pdfFileProblem(f) : '')
    setError(null)
  }

  const upload = async () => {
    if (!file || problem || busy) return
    setBusy(true)
    setError(null)
    setPct(0)
    try {
      const r = await uploadVendorDocument({ file, customer, onProgress: setPct })
      onUploaded(r && r.document_id)
    } catch (e) {
      setBusy(false)
      setError((e && e.message) || String(e))
    }
  }

  return (
    <section className="screen screen-capture screen-doc-upload">
      <header className="topbar">
        <button className="topbar-back" onClick={onBack} aria-label="Back" disabled={busy}>←</button>
        <div className="topbar-text">
          <span className="topbar-eyebrow">{step || 'Vendor document'}</span>
          <h2 className="topbar-title">Upload vendor document</h2>
        </div>
      </header>

      <CustomerProfileCard customer={customer} />

      <div className="doc-upload">
        <label className="doc-file-label" htmlFor="doc-file">
          Vendor PDF (quote, order preview or receipt), up to {MAX_PDF_BYTES / (1024 * 1024)} MB
        </label>
        <input
          id="doc-file"
          className="doc-file-input"
          type="file"
          accept="application/pdf,.pdf"
          onChange={choose}
          disabled={busy}
        />
        {file && (
          <p className="admin-note">
            {file.name}{sizeLabel(file.size) ? ` · ${sizeLabel(file.size)}` : ''}
          </p>
        )}
        {problem && <p className="admin-note is-error">{problem}</p>}
        {busy && (
          <p className="admin-note">
            {pct < 1 ? `Uploading… ${Math.round(pct * 100)}%` : 'Uploaded. Submitting…'}
          </p>
        )}
        {error && (
          <div className="admin-alert" role="alert">
            <strong>Upload failed.</strong> {error}
          </div>
        )}
      </div>

      <div className="footer-actions">
        <button
          className="btn btn-primary btn-lg"
          onClick={upload}
          disabled={!file || !!problem || busy}
        >
          {busy ? 'Uploading…' : 'Upload and read'}
        </button>
        <p className="footer-hint">
          The document is read in the background, then added to this customer&rsquo;s
          combined vendor material list (sent to FieldPulse only when the write mode allows).
        </p>
      </div>
    </section>
  )
}
