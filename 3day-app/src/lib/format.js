// Display formatting shared by screens. No network, no state.

const DATE_FORMAT = { month: 'short', day: 'numeric', year: 'numeric' }

// "Nov 3, 2026" from a customer's created_at; null if missing/bad.
// A date-only value ("2026-11-03") is a calendar date: build it as a local
// date so it never shifts a day. A full timestamp is shown in local time.
export function formatCustomerDate(createdAt) {
  if (typeof createdAt !== 'string' || !createdAt) return null
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(createdAt)
  const d = m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : new Date(createdAt)
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleDateString(undefined, DATE_FORMAT)
}
