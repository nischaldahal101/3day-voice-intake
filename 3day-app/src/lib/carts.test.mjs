// node --test src/lib/carts.test.mjs  (pure helpers; no DOM, no network). Made-up data.
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  FP_STATE_LABEL, cartBadge, cartMoney, cartStatusLabel, fpBusy, fpStateLabel, sendButtonText, sendCartPrompt, matchPending, matchText, count, deleteCartPrompt, expiryText, groupByRoom, itemTitle, itemTypeView, TYPE_UNKNOWN,
  lineTotal, parseCartsHash, qtyText, safeLink, toCents,
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
