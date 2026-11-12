// node --test src/lib/documents.test.mjs  (pure helpers; no DOM, no network)
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  canDeleteDocument, customerPageNeedsRefresh, customerRowNeedsRefresh, deleteDocumentPrompt, listStatusLabel,
  parseDocumentsHash,
} from './documents.js'

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

test('F-85: canDeleteDocument matches the server rule (not while being read, unless stuck)', () => {
  // Compact customer-page rows have no can_delete: status + stuck decide (documents_panel._can_delete).
  assert.equal(canDeleteDocument({ status: 'done' }), true)
  assert.equal(canDeleteDocument({ status: 'failed' }), true)
  assert.equal(canDeleteDocument({ status: 'queued' }), false)
  assert.equal(canDeleteDocument({ status: 'processing', stuck: false }), false)
  assert.equal(canDeleteDocument({ status: 'queued', stuck: true }), true)
  assert.equal(canDeleteDocument({ status: 'processing', stuck: true }), true)
  assert.equal(canDeleteDocument({ status: 'processing', stuck: 'yes' }), false)
  // The detail screen's server-computed can_delete wins.
  assert.equal(canDeleteDocument({ status: 'done', can_delete: false }), false)
  assert.equal(canDeleteDocument({ status: 'processing', can_delete: true }), true)
  assert.equal(canDeleteDocument(null), false)
  assert.equal(canDeleteDocument('doc_1'), false)
})

test('F-85: deleteDocumentPrompt is the shared confirm text', () => {
  const t = deleteDocumentPrompt('quote.pdf', 'Jane Doe')
  assert.match(t, /^Delete this vendor document\?/)
  assert.match(t, /File: quote\.pdf\n/)
  assert.match(t, /Customer: Jane Doe\n/)
  assert.match(t, /It cannot be undone\.$/)
  const blank = deleteDocumentPrompt(null, '  ')
  assert.match(blank, /File: unknown\n/)
  assert.match(blank, /Customer: none\n/)
})
