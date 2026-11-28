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

test('a cancelled version stays blocked until a new confirmation', () => {
  const base = { confirmedVersion: 1, draftVersion: 1, acknowledged: true, sentVersion: 0, cancelledVersion: 1 }
  // 取消后同一版本不可再发送（TASK-045 复审修复项）
  assert.equal(canSend(base), false)
  // 重新确认同一版本不放行；修改草稿并确认新版本后才生效
  assert.equal(canSend({ ...base, draftVersion: 2 }), false)
  assert.equal(canSend({ ...base, draftVersion: 2, confirmedVersion: 2 }), true)
  // 取消发生在确认任何版本之前时不锁定后续新版本
  assert.equal(canSend({ ...base, cancelledVersion: 0 }), true)
})