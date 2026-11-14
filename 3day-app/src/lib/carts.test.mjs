// node --test src/lib/carts.test.mjs  (pure helpers; no DOM, no network). Made-up data.
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  FP_STATE_LABEL, cartBadge, cartMoney, cartStatusLabel, fpBusy, fpStateLabel, sendButtonText, sendCartPrompt, matchPending, matchText, count, deleteCartPrompt, expiryText, groupByRoom, itemTitle, itemTypeView, TYPE_UNKNOWN,
  lineTotal, parseCartsHash, qtyText, safeLink, toCents,
  SEND_WAIT_MAX_ERRORS, SEND_WAIT_MAX_POLLS, autoSendExpected, autoSendWait, manualSendWait, sendWaitStep,
  shouldWaitForSend, waitKey,
} from './carts.js'

test('hashes', () => {
  assert.deepEqual(parseCartsHash('#carts'), { view: 'list' })
  assert.deepEqual(parseCartsHash('#carts/'), { view: 'list' })
  assert.deepEqual(parseCartsHash('#carts/cart_0123456789ab'), { view: 'detail', cartId: 'cart_0123456789ab' })
  for (const bad of ['#carts/Pat Example', '#carts/cart_XYZ', '#cartsx', '#documents', '', null, '#carts/cart_0123456789ab/x']) {
    assert.equal(parseCartsHash(bad), null, String(bad))
  }
})

test('money is exact from decimal strings', () => {
  assert.equal(toCents('1502.76'), 150276)
  assert.equal(toCents('46.67'), 4667)
  assert.equal(toCents('12'), 1200)
  assert.equal(toCents('12.5'), 1250)
  for (const bad of [null, 12.5, '', '-1.00', '1e3', '$12', '1,000.00', 'abc']) assert.equal(toCents(bad), null)
  assert.equal(cartMoney('1502.76'), '$1,502.76')
  assert.equal(cartMoney(null), '—')
  assert.equal(lineTotal(2, '46.67'), '$93.34')
  assert.equal(lineTotal(3, '0.10'), '$0.30') // no float drift
  assert.equal(lineTotal(null, '46.67'), '—')
  assert.equal(lineTotal(2, null), '—')
  assert.equal(lineTotal(1.5, '1.00'), '—')
  assert.equal(qtyText(2), '2')
  assert.equal(qtyText('2'), '—')
})

test('grouping, titles, links, labels', () => {
  const g = groupByRoom([
    { room: 'bathroom', brand: 'Testbrand', model: 'TB-1' },
    { room: 'Kitchen', model: 'OB-2' },
    { room: ' bathroom ', brand: 'Testbrand', model: 'TB-3' },
    { room: null, model: 'X' },
    null, 'junk', [],
  ])
  assert.deepEqual(g.map((x) => [x.room, x.items.length]), [['bathroom', 2], ['Kitchen', 1], ['Other', 1]])
  assert.deepEqual(groupByRoom(null), [])
  assert.equal(itemTitle({ brand: 'Testbrand', model: 'TB-1' }), 'Testbrand TB-1')
  assert.equal(itemTitle({ model: 'TB-1' }), 'TB-1')
  assert.equal(itemTitle({}), '—')
  assert.equal(safeLink('https://www.fergusonhome.com/x/p1'), 'https://www.fergusonhome.com/x/p1')
  for (const bad of ['javascript:alert(1)', 'http://x.invalid/', 'data:text/html,hi', '', null, 'nope']) {
    assert.equal(safeLink(bad), null, String(bad))
  }
  assert.equal(cartStatusLabel('needs_customer'), 'Needs customer')
  assert.equal(cartStatusLabel('bogus'), 'Unknown')
  assert.equal(count(3), 3)
  assert.equal(count(-1), null)
  assert.equal(count('3'), null)
  assert.equal(expiryText('2026-12-10'), '12/10/2026')
  assert.equal(expiryText(null), '—')
  assert.match(deleteCartPrompt('Testproject'), /Project: Testproject/)
  assert.match(deleteCartPrompt(null), /Project: unknown/)
})

test('E3: badge, match text, pending', () => {
  assert.equal(cartBadge('needs_customer', null), 'Needs customer')
  assert.equal(cartBadge('linked', '  Pat Example '), 'Linked: Pat Example')
  assert.equal(cartBadge('linked', ''), 'Linked')
  assert.equal(cartBadge('linked', 7), 'Linked')
  assert.equal(cartBadge('needs_customer', 'Pat Example'), 'Needs customer')
  assert.equal(cartBadge('sent', 'Pat'), 'Linked: Pat')   // E4: a sent cart stays linked
  assert.equal(matchText({ status: 'needs_customer', match_state: 'pending' }),
    'Looking for a matching FieldPulse customer…')
  assert.equal(matchText({ status: 'needs_customer', match_state: 'done', match_reason: '2 customers match.' }),
    '2 customers match.')
  assert.equal(matchText({ status: 'needs_customer' }), 'Not matched yet.')
  assert.equal(matchText({ status: 'linked', match_reason: 7 }), null)
  assert.equal(matchText(null), null)
  assert.equal(matchPending({ status: 'needs_customer', match_state: 'pending' }), true)
  assert.equal(matchPending({ status: 'linked', match_state: 'pending' }), false)
  assert.equal(matchPending({ status: 'needs_customer', match_state: 'error' }), false)
  assert.equal(matchPending(null), false)
})

test('E4: FieldPulse state labels, button, busy, prompt', () => {
  assert.equal(fpStateLabel('sent'), 'Sent')
  assert.equal(fpStateLabel('attention'), 'Needs attention')
  assert.equal(fpStateLabel('superseded'), 'Replaced by a newer cart')
  assert.equal(fpStateLabel('bogus'), 'Not sent')
  assert.equal(fpStateLabel(undefined), 'Not sent')
  for (const k of ['not_sent', 'dry_run', 'sent', 'attention']) assert.ok(FP_STATE_LABEL[k])
  assert.equal(sendButtonText('attention'), 'Retry')
  assert.equal(sendButtonText('dry_run'), 'Send to FieldPulse')
  assert.equal(fpBusy({ state: 'sending' }), true)
  assert.equal(fpBusy({ state: 'queued' }), true)
  assert.equal(fpBusy({ state: 'sent' }), false)
  assert.equal(fpBusy(null), false)
  assert.equal(cartBadge('sent', 'Pat Example'), 'Linked: Pat Example')
  assert.equal(cartStatusLabel('sent'), 'Sent')
  const p = sendCartPrompt('Testproject', 'Pat Example')
  assert.ok(p.includes('Testproject') && p.includes('Pat Example') && p.includes("can't be changed"))
  assert.ok(sendCartPrompt(null, null).includes('unknown'))
})

test('item type view: not decided / typed / flagged', () => {
  assert.deepEqual(itemTypeView({ item_type: 'Faucet' }, null), { text: '—', flag: false })
  const info = { source: 'ai', at: '2026-11-12T00:00:00Z' }
  assert.deepEqual(itemTypeView({ item_type: 'Faucet' }, info), { text: 'Faucet', flag: false })
  for (const it of [{ item_type: null }, { item_type: '  ' }, { item_type: 3 }, {}, null]) {
    assert.deepEqual(itemTypeView(it, info), { text: TYPE_UNKNOWN, flag: true })
  }
  assert.deepEqual(itemTypeView({ item_type: null }, { source: 'unavailable' }), { text: TYPE_UNKNOWN, flag: true })
})

test('F-110: a link / queued / sending / not-yet-recorded linked cart waits for the worker', () => {
  const linkedNoRecord = { status: 'linked', fieldpulse: { state: 'not_sent', raw_status: null, updated_at: null, problem: null } }
  assert.equal(autoSendExpected(linkedNoRecord), true)
  assert.equal(autoSendExpected({ status: 'linked' }), true)
  assert.equal(autoSendExpected({ ...linkedNoRecord, superseded_by: 'cart_0123456789ab' }), false)
  assert.equal(autoSendExpected({ ...linkedNoRecord, status: 'needs_customer' }), false)
  assert.equal(autoSendExpected({ ...linkedNoRecord, fieldpulse: { ...linkedNoRecord.fieldpulse, problem: 'Not sent: x' } }), false)
  // an earlier record (e.g. skipped / dry run) is not an automatic wait by itself
  assert.equal(autoSendExpected({ status: 'linked', fieldpulse: { state: 'not_sent', raw_status: 'skipped', updated_at: 't1' } }), false)
  for (const state of ['queued', 'sending']) {
    assert.equal(shouldWaitForSend({ status: 'linked', fieldpulse: { state, updated_at: 't1' } }), true)
  }
  assert.equal(shouldWaitForSend({ status: 'sent', fieldpulse: { state: 'sent', updated_at: 't1' } }), false)
  assert.equal(shouldWaitForSend(null), false)

  // one automatic wait per record
  const w = autoSendWait(linkedNoRecord, null)
  assert.deepEqual(w, { since: null, polls: 0, errors: 0, auto: true, key: 'not_sent|null' })
  assert.equal(autoSendWait(linkedNoRecord, w.key), null)
  const queued = { status: 'linked', fieldpulse: { state: 'queued', updated_at: 't1' } }
  assert.equal(autoSendWait(queued, w.key).since, 't1')

  // link -> queued -> sending -> sent, no manual refresh
  let s = sendWaitStep(w, queued)
  assert.deepEqual(s, { done: false, polls: 1, errors: 0, note: null })
  s = sendWaitStep({ ...w, polls: s.polls }, { fieldpulse: { state: 'sending', updated_at: 't2' } })
  assert.equal(s.done, false)
  s = sendWaitStep({ ...w, polls: s.polls }, { fieldpulse: { state: 'sent', updated_at: 't3' } })
  assert.deepEqual(s, { done: true, polls: 3, errors: 0, note: null })
  // ... or failed
  assert.equal(sendWaitStep(w, { fieldpulse: { state: 'attention', updated_at: 't3' } }).note,
    'Finished with a problem (see above).')
  // unchanged record: bounded; an automatic wait ends quietly, a manual one says so
  const stuck = { fieldpulse: { state: 'not_sent', updated_at: null } }
  assert.deepEqual(sendWaitStep({ ...w, polls: SEND_WAIT_MAX_POLLS - 1 }, stuck), { done: true, polls: SEND_WAIT_MAX_POLLS, errors: 0, note: null })
  const m = manualSendWait({ fieldpulse: { updated_at: 't0' } })
  assert.deepEqual(m, { since: 't0', polls: 0, errors: 0, auto: false, key: 'undefined|t0' })
  assert.match(sendWaitStep({ ...m, polls: SEND_WAIT_MAX_POLLS - 1 }, { fieldpulse: { updated_at: 't0' } }).note, /Still waiting/)
  // relink of a dry-run cart: the link wait starts from the current record and ends on the new one
  const relink = manualSendWait({ fieldpulse: { state: 'dry_run', updated_at: 't5' } }, true)
  assert.equal(sendWaitStep(relink, { fieldpulse: { state: 'dry_run', updated_at: 't5' } }).done, false)
  assert.equal(sendWaitStep(relink, { fieldpulse: { state: 'dry_run', updated_at: 't6' } }).done, true)
})

test('F-112: one polling budget per record (waitKey shared by link/manual/auto waits)', () => {
  const linked = { status: 'linked', fieldpulse: { state: 'not_sent', updated_at: null } }
  const link = manualSendWait(linked, true)
  assert.equal(link.key, waitKey(linked))
  // after the link wait ends on the unchanged record, no automatic wait restarts for it
  assert.equal(autoSendWait(linked, link.key), null)
  // a different record does get its own (bounded) wait
  const queued = { status: 'linked', fieldpulse: { state: 'queued', updated_at: 't1' } }
  assert.notEqual(autoSendWait(queued, link.key), null)
  assert.equal(autoSendWait(queued, waitKey(queued)), null)
})

test('F-112 R4: every key a wait saw stays consumed (a late older response cannot rearm)', () => {
  const queued = { status: 'linked', fieldpulse: { state: 'queued', updated_at: 'q' } }
  const sending = { status: 'sent', fieldpulse: { state: 'sending', updated_at: 's' } }
  const seen = new Set()
  const w = autoSendWait(queued, seen)
  seen.add(w.key)                       // the screen records the start key ...
  seen.add(waitKey(sending))            // ... and every key a reload during the wait saw
  assert.equal(autoSendWait(sending, seen), null)
  assert.equal(autoSendWait(queued, seen), null)   // the old queued record comes back late
  assert.notEqual(autoSendWait({ status: 'sent', fieldpulse: { state: 'sending', updated_at: 's2' } }, seen), null)
})

test('F-113: a failed reload keeps the wait going, bounded, then stops with a visible note', () => {
  const m = manualSendWait({ fieldpulse: { state: 'not_sent', updated_at: null } })
  const err = new Error('HTTP 503')
  let s = sendWaitStep(m, null, err)
  assert.deepEqual(s, { done: false, polls: 1, errors: 1, note: null })
  // transient error -> success: errors reset, wait continues / finishes normally
  s = sendWaitStep({ ...m, polls: s.polls, errors: s.errors }, { fieldpulse: { state: 'queued', updated_at: 't1' } })
  assert.deepEqual(s, { done: false, polls: 2, errors: 0, note: null })
  s = sendWaitStep({ ...m, polls: s.polls, errors: s.errors }, { fieldpulse: { state: 'sent', updated_at: 't2' } })
  assert.equal(s.done, true)
  // persistent error -> stops after SEND_WAIT_MAX_ERRORS in a row, with the reason
  let w = { ...m }
  for (let i = 1; i <= SEND_WAIT_MAX_ERRORS; i++) {
    s = sendWaitStep(w, null, err)
    w = { ...w, polls: s.polls, errors: s.errors }
    assert.equal(s.done, i === SEND_WAIT_MAX_ERRORS)
  }
  assert.match(s.note, /Couldn't check the send status \(HTTP 503\)\. Press Refresh/)
  // automatic waits show it too; the poll cap also bounds errors
  const a = autoSendWait({ status: 'linked', fieldpulse: null }, null)
  assert.match(sendWaitStep({ ...a, errors: SEND_WAIT_MAX_ERRORS - 1 }, null, err).note, /Couldn't check/)
  assert.equal(sendWaitStep({ ...a, polls: SEND_WAIT_MAX_POLLS - 1 }, null, err).done, true)
})
