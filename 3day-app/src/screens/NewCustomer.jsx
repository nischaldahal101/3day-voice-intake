import { useState } from 'react'
import { createCustomer } from '../lib/api.js'

const EMAIL_RE = /^\S+@\S+\.\S+$/

export default function NewCustomer({ onBack, onCreated }) {
  const [first, setFirst] = useState('')
  const [last, setLast] = useState('')
  const [phone, setPhone] = useState('')
  const [email, setEmail] = useState('')
  const [touched, setTouched] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const errors = {
    first: first.trim() ? null : 'First name is required',
    last: last.trim() ? null : 'Last name is required',
    phone: phone.trim() ? null : 'Phone number is required',
    email: email.trim() && !EMAIL_RE.test(email.trim()) ? 'Doesn’t look like an email' : null,
  }
  const isValid = !errors.first && !errors.last && !errors.phone && !errors.email

  async function submit() {
    setTouched(true)
    setError(null)
    if (!isValid) return
    setSaving(true)
    try {
      const customer = await createCustomer({
        first_name: first.trim(),
        last_name: last.trim(),
        phone: phone.trim(),
        email: email.trim(),
      })
      onCreated(customer)
    } catch (e) {
      setError((e && e.message) || 'Something went wrong.')
      setSaving(false)
    }
  }

  return (
    <section className="screen screen-new-customer">
      <header className="topbar">
        <button
          className="topbar-back"
          onClick={onBack}
          aria-label="Back"
          disabled={saving}
        >
          ←
        </button>
        <div className="topbar-text">
          <span className="topbar-eyebrow">New customer</span>
          <h2 className="topbar-title">Add details</h2>
        </div>
      </header>

      <div className="form-body">
        <div className="form-row">
          <label className="field-label" htmlFor="nc-first">First name</label>
          <input
            id="nc-first"
            className={`field-input ${touched && errors.first ? 'is-invalid' : ''}`}
            value={first}
            onChange={(e) => setFirst(e.target.value)}
            autoCapitalize="words"
            autoCorrect="off"
            autoComplete="given-name"
            disabled={saving}
          />
          {touched && errors.first && (
            <p className="field-error">{errors.first}</p>
          )}
        </div>

        <div className="form-row">
          <label className="field-label" htmlFor="nc-last">Last name</label>
          <input
            id="nc-last"
            className={`field-input ${touched && errors.last ? 'is-invalid' : ''}`}
            value={last}
            onChange={(e) => setLast(e.target.value)}
            autoCapitalize="words"
            autoCorrect="off"
            autoComplete="family-name"
            disabled={saving}
          />
          {touched && errors.last && (
            <p className="field-error">{errors.last}</p>
          )}
        </div>

        <div className="form-row">
          <label className="field-label" htmlFor="nc-phone">Phone number</label>
          <input
            id="nc-phone"
            type="tel"
            inputMode="tel"
            className={`field-input ${touched && errors.phone ? 'is-invalid' : ''}`}
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="618-555-0123"
            autoComplete="tel"
            disabled={saving}
          />
          {touched && errors.phone && (
            <p className="field-error">{errors.phone}</p>
          )}
        </div>

        <div className="form-row">
          <label className="field-label" htmlFor="nc-email">
            Email <span className="field-optional">optional</span>
          </label>
          <input
            id="nc-email"
            type="email"
            inputMode="email"
            className={`field-input ${touched && errors.email ? 'is-invalid' : ''}`}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="name@example.com"
            autoCapitalize="off"
            autoCorrect="off"
            autoComplete="email"
            disabled={saving}
          />
          {touched && errors.email && (
            <p className="field-error">{errors.email}</p>
          )}
        </div>

        {error && (
          <div className="form-error" role="alert">
            <strong>Couldn&rsquo;t save.</strong> {error}
          </div>
        )}
      </div>

      <div className="footer-actions footer-actions-stick">
        <button
          className="btn btn-primary btn-lg"
          onClick={submit}
          disabled={saving}
        >
          {saving ? 'Saving…' : 'Save & continue'}
        </button>
      </div>
    </section>
  )
}
