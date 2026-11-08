// node --test src/lib/documents.test.mjs  (pure helpers; no DOM, no network)
import test from 'node:test'
import assert from 'node:assert/strict'
import { customerPageNeedsRefresh, customerRowNeedsRefresh, listStatusLabel, parseDocumentsHash } from './documents.js'

test('F-80: refresh continues after reading ends while the list sync is pending or sending', () => {
  // Reading -> read done, sync queued (server says sync_pending) -> sending -> sent.
  const page = (over) => ({ processing_count: 0, sync_pending: false, fieldpulse: { status: 'not_sent' }, ...over })
  assert.equal(customerPageNeedsRefresh(page({ processing_count: 1 })), true)
  assert.equal(customerPageNeedsRefresh(page({ sync_pending: true })), true)
  assert.equal(customerPageNeedsRefresh(page({ fieldpulse: { status: 'sending' } })), true)
  assert.equal(customerPageNeedsRefresh(page({ fieldpulse: { status: 'sent' } })), false)
  assert.equal(customerPageNeedsRefresh(page({ fieldpulse: { status: 'attention' } })), false)
  assert.equal(customerPageNeedsRefresh(null), false)
  assert.equal(customerPageNeedsRefresh({ fieldpulse: null }), false)
  const row = (over) => ({ processing_count: 0, sync_pending: false, fieldpulse_status: 'not_sent', ...over })
  assert.equal(customerRowNeedsRefresh(row({ processing_count: 2 })), true)
  assert.equal(customerRowNeedsRefresh(row({ sync_pending: true })), true)
  assert.equal(customerRowNeedsRefresh(row({ fieldpulse_status: 'sending' })), true)
  assert.equal(customerRowNeedsRefresh(row({ fieldpulse_status: 'dry_run' })), false)
  assert.equal(customerRowNeedsRefresh(row({ sync_pending: 'yes' })), false)
})

test('labels and hashes', () => {
  assert.equal(listStatusLabel('held'), 'Held (dry run)')
  assert.equal(listStatusLabel('bogus'), 'Not sent')
  assert.deepEqual(parseDocumentsHash('#documents/c/18809123'), { view: 'customer', customerId: '18809123' })
})
