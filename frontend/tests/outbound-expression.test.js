import test from 'node:test'
import assert from 'node:assert/strict'

import { canSend, needsConfirmation, outboundSample } from '../src/outbound-expression.js'

test('keeps intent and draft separately visible', () => {
  assert.match(outboundSample.intent, /84 元/u)
  assert.match(outboundSample.draft, /周五前/u)
})

test('requires a new confirmation after the draft version changes', () => {
  assert.equal(needsConfirmation(1, 1), false)
  assert.equal(needsConfirmation(1, 2), true)
})

test('only sends an acknowledged, confirmed, unsent version', () => {
  const base = { confirmedVersion: 1, draftVersion: 1, acknowledged: true, sentVersion: 0 }
  assert.equal(canSend(base), true)
  assert.equal(canSend({ ...base, acknowledged: false }), false)
  assert.equal(canSend({ ...base, draftVersion: 2 }), false)
  assert.equal(canSend({ ...base, sentVersion: 1 }), false)
})