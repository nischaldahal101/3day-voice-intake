// Admin panel display helpers. Pure: no network, no React state.
// Everything here re-guards server JSON (any field can be missing or odd).

export const BRANCH_TZ = 'America/Chicago'

const DATE_TIME = {
  timeZone: BRANCH_TZ, month: 'short', day: 'numeric', year: 'numeric',
  hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
}

// "Nov 3, 2026, 2:00 PM CST" in the branch timezone; null if missing/bad.
export function fmtDateTime(iso) {
  if (typeof iso !== 'string' || !iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleString('en-US', DATE_TIME)
}

// Epoch seconds (FieldPulse task times) -> branch-time string.
export function fmtEpoch(sec) {
  if (typeof sec !== 'number' || !Number.isFinite(sec)) return null
  return new Date(sec * 1000).toLocaleString('en-US', DATE_TIME)
}

export const FP_LABEL = {
  succeeded: 'Sent to FieldPulse',
  partial: 'Partly sent',
  failed: 'Failed',
  sent: 'Sent, no answer',
  held: 'Held (send by hand)',
  dry_run: 'Dry run (not sent)',
  not_sent: 'Not sent',
  pending: 'Pending',
  not_attempted: 'Not attempted',
  legacy_mock: 'Legacy (mock only)',
}

export const FP_FILTERS = [
  ['', 'All'],
  ['attention', 'Needs attention'],
  ['succeeded', 'Sent'],
  ['dry_run', 'Dry run'],
  ['held', 'Held'],
  ['failed', 'Failed'],
  ['partial', 'Partly sent'],
  ['sent', 'Sent, no answer'],
  ['not_sent', 'Not sent'],
  ['pending', 'Pending'],
  ['not_attempted', 'Not attempted'],
  ['legacy_mock', 'Legacy (mock)'],
]

export const SOURCE_LABEL = {
  record: 'Recorded',
  upload: 'Uploaded',
  paste: 'Pasted',
  audio: 'Audio (record or upload)',
  unknown: 'Unknown',
}

export const STEP_NAME = {
  comment: 'Comment on the customer',
  return_task: 'Return-appointment task',
  material_list: 'Draft material list',
}

export const WRITE_STATUS_LABEL = {
  planned: 'Planned, not sent',
  sent: 'Sent, no answer',
  succeeded: 'Succeeded',
  failed: 'Failed',
  skipped: 'Skipped',
  held: 'Held (dry run)',
}

export const MODE_LABEL = {
  dry_run: 'dry run',
  off: 'off',
  test_customer_only: 'test customer only',
  live: 'live',
}

export function str(v) {
  return typeof v === 'string' && v.trim() ? v : null
}

export function isObj(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

export function humanize(key) {
  const s = String(key).replace(/_/g, ' ').trim()
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : String(key)
}

export function pretty(v) {
  try { return JSON.stringify(v, null, 2) } catch (_) { return String(v) }
}

// Any value -> display text (null = nothing there).
export function display(v) {
  if (v === null || v === undefined) return null
  if (typeof v === 'string') return v.trim() ? v : null
  if (typeof v === 'boolean') return v ? 'Yes' : 'No'
  if (typeof v === 'number') return Number.isFinite(v) ? String(v) : null
  if (Array.isArray(v)) {
    const parts = v.map(display).filter(Boolean)
    return parts.length ? parts.join('; ') : null
  }
  if (isObj(v)) return Object.keys(v).length ? pretty(v) : null
  return String(v)
}

// "Not mentioned" per the prompt: null, empty, or "unknown".
export function isMissing(v) {
  if (v === null || v === undefined) return true
  if (typeof v === 'string') return !v.trim() || v.trim().toLowerCase() === 'unknown'
  if (Array.isArray(v)) return v.length === 0
  return false
}

// ---- value formatters for known fields ------------------------------------

const YES_NO = { yes: 'Yes', no: 'No', true: 'Yes', false: 'No' }
const APPLIANCE = { new: 'New', reinstall: 'Keeping / reinstalling', none: 'Not part of the project' }

function money(v) {
  const n = typeof v === 'number' ? v : NaN
  return Number.isFinite(n)
    ? n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
    : display(v)
}

function yesNo(v) {
  if (typeof v === 'boolean') return v ? 'Yes' : 'No'
  return YES_NO[String(v).trim().toLowerCase()] || display(v)
}

function appliance(v) {
  return APPLIANCE[String(v).trim().toLowerCase()] || display(v)
}

function isoDay(v) {
  const m = typeof v === 'string' && /^(\d{4})-(\d{2})-(\d{2})$/.exec(v.trim())
  if (!m) return display(v)
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
  if (Number.isNaN(d.getTime())) return display(v)
  return d.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })
}

function hhmm(v) {
  const m = typeof v === 'string' && /^([01]?\d|2[0-3]):([0-5]\d)/.exec(v.trim())
  if (!m) return display(v)
  const h = Number(m[1])
  return `${h % 12 || 12}:${m[2]} ${h < 12 ? 'AM' : 'PM'}`
}

const fmt = {
  text: display,
  yesNo,
  appliance,
  money,
  percent: (v) => (typeof v === 'number' && Number.isFinite(v) ? `${v}%` : display(v)),
  score: (v) => (typeof v === 'number' && Number.isFinite(v) ? `${v} / 10` : display(v)),
  project: (v) => (typeof v === 'string' && v.trim() ? humanize(v) : display(v)),
  day: isoDay,
  time: hhmm,
}

// [key, label, formatter, keywords that link an AI flag to this field]
const GROUPS = [
  {
    id: 'summary', title: 'Summary', path: [], fields: [
      ['notes_summary', 'Summary', fmt.text, ['summary']],
      ['project_type', 'Project type', fmt.project, ['project type', 'kitchen or bath']],
      ['match_confidence', 'Customer match confidence', fmt.text, ['match', 'confidence']],
    ],
  },
  {
    id: 'client', title: 'Customer as heard in the consultation', path: ['client'], fields: [
      ['name', 'Name', fmt.text, ['name']],
      ['phone', 'Phone', fmt.text, ['phone']],
      ['email', 'Email', fmt.text, ['email']],
    ],
  },
  {
    id: 'scope', title: 'Scope', path: ['scope'], fields: [
      ['project_vision', 'Project vision', fmt.text, ['vision', 'in mind']],
      ['structural_changes', 'Structural changes', fmt.yesNo, ['structural', 'wall']],
      ['structural_details', 'Structural details', fmt.text, ['structural', 'wall']],
      ['plumbing_changes', 'Plumbing changes', fmt.yesNo, ['plumbing']],
      ['plumbing_details', 'Plumbing details', fmt.text, ['plumbing']],
      ['electrical_changes', 'Electrical changes', fmt.yesNo, ['electrical', 'panel', 'outlet']],
      ['electrical_details', 'Electrical details', fmt.text, ['electrical', 'panel', 'outlet']],
      ['lighting_changes', 'Lighting changes', fmt.yesNo, ['lighting', 'light']],
      ['lighting_details', 'Lighting details', fmt.text, ['lighting', 'light']],
      ['style', 'Style / feeling', fmt.text, ['style', 'feeling', 'look']],
    ],
    nested: ['appliances'],
  },
  {
    id: 'appliances', title: 'Appliances', path: ['scope', 'appliances'], fields: [
      ['refrigerator', 'Refrigerator', fmt.appliance, ['refrigerator', 'fridge', 'appliance']],
      ['range', 'Range', fmt.appliance, ['range', 'stove', 'appliance']],
      ['dishwasher', 'Dishwasher', fmt.appliance, ['dishwasher', 'appliance']],
      ['microwave', 'Microwave', fmt.appliance, ['microwave', 'appliance']],
      ['hood', 'Hood', fmt.appliance, ['hood', 'appliance']],
      ['other', 'Other', fmt.text, ['appliance']],
    ],
  },
  {
    id: 'readiness', title: 'Timeline & readiness', path: ['readiness'], fields: [
      ['time_frame', 'Time frame', fmt.text, ['time frame', 'time-frame', 'timeline', 'timing']],
      ['thinking_duration', 'How long they have been thinking about it', fmt.text,
        ['thinking', 'how long', 'why now']],
      ['work_done_so_far', 'Work done so far (plans, products)', fmt.text,
        ['plans', 'products', 'work done', 'drawings']],
    ],
  },
  {
    id: 'sales', title: 'Sales', path: ['sales'], fields: [
      ['close_score_1_10', 'Close score (1-10)', fmt.score, ['close score', '1-10', '1 to 10']],
      ['likelihood_percent', 'Likelihood', fmt.percent, ['likelihood', 'chance', 'percent', '%']],
      ['budget_raw', 'Budget (as said)', fmt.text, ['budget']],
      ['budget_low', 'Budget low', fmt.money, ['budget']],
      ['budget_high', 'Budget high', fmt.money, ['budget']],
      ['waiting_on_financing', 'Waiting on financing', fmt.yesNo, ['financing', 'finance']],
      ['financing_details', 'Financing details', fmt.text, ['financing', 'finance']],
      ['concerns', 'Concerns', fmt.text, ['concern', 'worr']],
      ['process_presentation_shown', 'Process presentation (UFC) shown', fmt.yesNo,
        ['presentation', 'ufc', 'process']],
    ],
  },
  {
    id: 'return', title: 'Return appointment', path: ['return_appointment'], fields: [
      ['scheduled', 'Scheduled', fmt.yesNo, ['return', 'appointment']],
      ['details', 'As said', fmt.text, ['return', 'appointment']],
      ['resolved_date', 'Date worked out', fmt.day, ['return', 'appointment', 'date']],
      ['resolved_time', 'Time worked out', fmt.time, ['return', 'appointment', 'time', 'am/pm']],
    ],
  },
]

const TOP_KNOWN = new Set([
  'project_type', 'match_confidence', 'client', 'scope', 'readiness', 'sales',
  'return_appointment', 'notes_summary', 'raw_quotes', 'flags',
])

function at(obj, path) {
  let cur = obj
  for (const p of path) {
    if (!isObj(cur)) return undefined
    cur = cur[p]
  }
  return cur
}

function flagsFor(flags, keywords) {
  return flags.filter((f) => {
    const t = f.toLowerCase()
    return keywords.some((k) => t.includes(k))
  })
}

function genericRow(key, value) {
  return {
    key, label: humanize(key), value: display(value), missing: isMissing(value),
    flags: [], raw: isObj(value) || Array.isArray(value),
  }
}

// The extraction as display groups:
// [{ id, title, rows: [{key, label, value, missing, flags, raw?}], note? }]
// Every field is present (missing ones as "Not mentioned" + matching AI flags),
// plus "Other fields" for anything the code doesn't know, so nothing is hidden.
export function groupExtraction(ex) {
  if (!isObj(ex)) return { groups: [], quotes: [], flags: [] }
  const flags = (Array.isArray(ex.flags) ? ex.flags : []).map(display).filter(Boolean)
  const quotes = (Array.isArray(ex.raw_quotes) ? ex.raw_quotes : []).map(display).filter(Boolean)
  const groups = []
  const other = []

  for (const g of GROUPS) {
    const holder = g.path.length ? at(ex, g.path) : ex
    const rows = []
    let note = null
    let src = isObj(holder) ? holder : {}
    if (g.id === 'appliances' && holder == null) {
      note = String(ex.project_type).toLowerCase() === 'bath'
        ? 'Not applicable (bath project).'
        : 'Not mentioned.'
      src = null
    } else if (g.path.length && !isObj(holder) && holder != null) {
      // The group is there but isn't an object: show it as-is, fields below as missing.
      rows.push(genericRow(g.path[g.path.length - 1], holder))
    }
    if (src) {
      const known = new Set(g.fields.map((f) => f[0]).concat(g.nested || []))
      for (const [key, label, f, kw] of g.fields) {
        const v = src[key]
        const missing = isMissing(v)
        rows.push({
          key, label, missing,
          value: missing ? null : (f(v) ?? display(v)),
          unknownAnswer: typeof v === 'string' && v.trim().toLowerCase() === 'unknown',
          flags: missing ? flagsFor(flags, kw) : [],
        })
      }
      if (g.path.length) {
        for (const key of Object.keys(src)) {
          if (!known.has(key)) rows.push(genericRow(key, src[key]))
        }
      }
    }
    groups.push({ id: g.id, title: g.title, rows, note })
  }

  for (const key of Object.keys(ex)) {
    if (!TOP_KNOWN.has(key)) other.push(genericRow(key, ex[key]))
  }
  if (ex.raw_quotes !== undefined && !Array.isArray(ex.raw_quotes)) {
    other.push(genericRow('raw_quotes', ex.raw_quotes))
  }
  if (ex.flags !== undefined && !Array.isArray(ex.flags)) other.push(genericRow('flags', ex.flags))
  if (other.length) groups.push({ id: 'other', title: 'Other fields', rows: other, note: null })
  return { groups, quotes, flags }
}

// ---- plain summary for the top of the detail screen ------------------------

export const JOB_STATUS_LABEL = {
  queued: 'Waiting to start',
  processing: 'Processing',
  done: 'Done',
  failed: 'Failed',
}

// Same set as the backend's admin_panel.ATTENTION (FieldPulse side only; a
// failed job is shown by its job status, not as a FieldPulse problem).
const FP_ATTENTION = new Set(['failed', 'partial', 'held', 'sent'])

// One plain line: { kind: 'sent'|'attention'|'pending'|'not_sent', text, detail }.
export function fieldPulsePlain(d) {
  const fp = isObj(d) ? d.fieldpulse_status : null
  const label = (isObj(d) && str(d.fieldpulse_label)) || FP_LABEL[fp] || null
  if (FP_ATTENTION.has(fp)) return { kind: 'attention', text: 'Needs attention', detail: label }
  if (fp === 'succeeded') return { kind: 'sent', text: 'Sent', detail: null }
  if (fp === 'pending') return { kind: 'pending', text: 'Not sent yet', detail: 'Still processing' }
  return { kind: 'not_sent', text: 'Not sent', detail: fp === 'not_sent' ? null : label }
}

function budget(sales) {
  const lo = isMissing(sales.budget_low) ? null : money(sales.budget_low)
  const hi = isMissing(sales.budget_high) ? null : money(sales.budget_high)
  if (lo && hi) return lo === hi ? lo : `${lo} to ${hi}`
  if (lo) return `From ${lo}`
  if (hi) return `Up to ${hi}`
  return isMissing(sales.budget_raw) ? null : display(sales.budget_raw)
}

function returnAppointment(ra) {
  const said = isMissing(ra.details) ? null : display(ra.details)
  if (!isMissing(ra.resolved_date)) {
    const when = [isoDay(ra.resolved_date), isMissing(ra.resolved_time) ? null : hhmm(ra.resolved_time)]
      .filter(Boolean).join(' at ')
    return said ? `${when} (said: "${said}")` : when
  }
  const sch = String(ra.scheduled).trim().toLowerCase()
  if (said) return said
  if (ra.scheduled === true || sch === 'true' || sch === 'yes') return 'Scheduled, no date or time captured'
  if (ra.scheduled === false || sch === 'false' || sch === 'no') return 'Not scheduled'
  return null
}

// Materials as said, grouped by area ("Other" last), one line per area:
// "Shower: subway tile (white 3x6; 120 sq ft); glass door". null = none
// mentioned; a missing key (older consultations) says so instead.
export function materialsText(ex) {
  if (!isObj(ex) || !('materials' in ex)) return 'Not captured (older consultation)'
  if (!Array.isArray(ex.materials)) return null
  const groups = new Map()
  for (const m of ex.materials) {
    if (!isObj(m) || !str(m.item)) continue
    const area = str(m.area) ? m.area.trim() : 'Other'
    const key = area.toLowerCase()
    if (!groups.has(key)) groups.set(key, { area, items: [] })
    const qty = typeof m.quantity === 'number' && Number.isFinite(m.quantity) && m.quantity > 0
      ? [m.quantity, str(m.unit)].filter(Boolean).join(' ') : null
    const extra = [str(m.details), qty].filter(Boolean).join('; ')
    groups.get(key).items.push(extra ? `${m.item.trim()} (${extra})` : m.item.trim())
  }
  const other = groups.get('other')
  groups.delete('other')
  const ordered = [...groups.values(), ...(other ? [other] : [])]
  return ordered.length ? ordered.map((g) => `${g.area}: ${g.items.join('; ')}`).join('\n') : null
}

// The few results Mike reads first. Each: { key, label, value } with value
// null = "Not mentioned" (never filled in). The full list stays in
// groupExtraction for the troubleshooting section.
export function keyResults(ex) {
  if (!isObj(ex)) return null
  const sales = isObj(ex.sales) ? ex.sales : {}
  const scope = isObj(ex.scope) ? ex.scope : {}
  const ready = isObj(ex.readiness) ? ex.readiness : {}
  const ra = isObj(ex.return_appointment) ? ex.return_appointment : {}
  const v = (x, f) => (isMissing(x) ? null : (f(x) ?? display(x)))
  return {
    stats: [
      { key: 'close', label: 'Close score', value: v(sales.close_score_1_10, fmt.score) },
      { key: 'likelihood', label: 'Likelihood', value: v(sales.likelihood_percent, fmt.percent) },
      { key: 'budget', label: 'Budget', value: budget(sales) },
    ],
    rows: [
      { key: 'summary', label: 'Summary', value: v(ex.notes_summary, fmt.text) },
      { key: 'project', label: 'Project', value: v(ex.project_type, fmt.project) },
      { key: 'vision', label: 'Project vision', value: v(scope.project_vision, fmt.text) },
      { key: 'timeline', label: 'Timeline', value: v(ready.time_frame, fmt.text) },
      { key: 'return', label: 'Return appointment', value: returnAppointment(ra) },
      { key: 'concerns', label: 'Concerns', value: v(sales.concerns, fmt.text) },
      { key: 'financing', label: 'Financing', value: v(sales.financing_details, fmt.text) },
      { key: 'materials', label: 'Materials', value: materialsText(ex) },
    ],
  }
}

// Copy to clipboard; falls back to a hidden textarea (older iOS / http).
export async function copyText(text) {
  if (typeof text !== 'string') return false
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch (_) { /* fall through */ }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch (_) {
    return false
  }
}

// Admin URL helpers: #admin (list) and #admin/<job_id> (detail). A hash works on
// any static host without rewrite rules, survives Clerk sign-in, and matches
// the link in the FieldPulse comment footer (ADMIN_BASE_URL/#admin/<id>).
// #consultations[/<id>] is accepted as a friendlier alias; the app itself
// keeps writing #admin so old bookmarks and FieldPulse links stay identical.
export function parseAdminHash(hash) {
  const h = typeof hash === 'string' ? hash.replace(/^#\/?/, '') : ''
  if (/^(admin|consultations)\/?$/.test(h)) return { view: 'list' }
  const m = /^(?:admin|consultations)\/([A-Za-z0-9_-]{1,64})$/.exec(h)
  if (m) return { view: 'detail', jobId: m[1] }
  return null
}
