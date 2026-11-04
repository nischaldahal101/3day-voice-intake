import { formatCustomerDate } from '../lib/format.js'

// String or null. The customer can come from fetchCustomers (already
// normalised) or from an older localStorage active job (missing fields, or
// anything else), so nothing here trusts the shape.
function text(v) {
  if (typeof v === 'string') return v.trim() || null
  if (typeof v === 'number' && Number.isFinite(v)) return String(v)
  return null
}

// tel: target with only digits and a leading +, or null if there are none.
// Anything after an extension marker ("x2", "ext. 5", "#5") is dropped so it
// isn't dialled as part of the number.
function telHref(phone) {
  const main = phone.split(/\s*(?:ext\.?|x|#|;|,)/i)[0]
  const digits = main.replace(/[^\d+]/g, '').replace(/(?!^)\+/g, '')
  return /\d/.test(digits) ? `tel:${digits}` : null
}

const EMAIL_RE = /^[^\s@<>"]+@[^\s@<>"]+$/

// Read-only "who is this?" card on the Capture screen. Renders nothing
// without a customer; each row only when its value exists.
export default function CustomerProfileCard({ customer }) {
  if (!customer || typeof customer !== 'object') return null
  const name =
    text(customer.display_name) ||
    `${text(customer.first_name) || ''} ${text(customer.last_name) || ''}`.trim() ||
    null
  const company = text(customer.company_name)
  const phone = text(customer.phone)
  const email = text(customer.email)
  const address = text(customer.address)
  const since = formatCustomerDate(text(customer.created_at))
  const id = text(customer.id)
  if (!name && !company && !phone && !email && !address && !since && !id) return null

  const tel = phone && telHref(phone)
  return (
    <aside className="profile-card" aria-label="Customer details">
      {name && <p className="profile-name">{name}</p>}
      {company && <p className="profile-company">{company}</p>}
      <dl className="profile-rows">
        {phone && (
          <div className="profile-row">
            <dt>Phone</dt>
            <dd>{tel ? <a href={tel}>{phone}</a> : phone}</dd>
          </div>
        )}
        {email && (
          <div className="profile-row">
            <dt>Email</dt>
            <dd>{EMAIL_RE.test(email) ? <a href={`mailto:${email}`}>{email}</a> : email}</dd>
          </div>
        )}
        {address && (
          <div className="profile-row">
            <dt>Address</dt>
            <dd>{address}</dd>
          </div>
        )}
      </dl>
      {since && <p className="profile-since">Customer since {since}</p>}
      {id && <p className="profile-id">FieldPulse ID {id}</p>}
    </aside>
  )
}
