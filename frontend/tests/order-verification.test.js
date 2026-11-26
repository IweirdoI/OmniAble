import test from 'node:test'
import assert from 'node:assert/strict'

import { needsRecheck, parseOrderFields, validateOrderFields } from '../src/order-verification.js'

const confirmed = '# 客户确认单\n\n商品：蓝色收纳盒\n数量：3 件\n单价：28 元\n确认金额：84 元\n'
const draft = '# 待核验订单\n\n商品：蓝色收纳盒\n数量：2 件\n单价：28 元\n金额：56 元\n'

test('parses the synthetic order and confirmed source fields', () => {
  assert.deepEqual(parseOrderFields(confirmed), {
    product: '蓝色收纳盒', quantity: 3, unitPrice: 28, amount: 84,
  })
  assert.deepEqual(parseOrderFields(draft), {
    product: '蓝色收纳盒', quantity: 2, unitPrice: 28, amount: 56,
  })
})

test('requires correcting both quantity and amount to the confirmed source', () => {
  const expected = parseOrderFields(confirmed)
  assert.deepEqual(
    validateOrderFields({ ...parseOrderFields(draft), quantity: 3, amount: 84 }, expected),
    [],
  )
  const errors = validateOrderFields({ ...parseOrderFields(draft), quantity: 2, amount: 56 }, expected)
  assert(errors.some(message => message.includes('数量应改为 3')))
  assert(errors.some(message => message.includes('金额应改为 84')))
})

test('source version changes require recheck', () => {
  assert.equal(needsRecheck(1, 1), false)
  assert.equal(needsRecheck(1, 2), true)
})