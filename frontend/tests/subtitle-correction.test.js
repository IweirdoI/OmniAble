import test from 'node:test'
import assert from 'node:assert/strict'

import { applySubtitleCorrection, correctionDiff, subtitleSample } from '../src/subtitle-correction.js'

test('keeps the original business fields until the correction is applied', () => {
  const business = { product: '蓝色收纳袋', quantity: 2 }
  const correction = { product: subtitleSample.confirmedProduct, quantity: subtitleSample.confirmedQuantity }
  assert.deepEqual(correctionDiff(business, correction), [
    { field: '商品', before: '蓝色收纳袋', after: '蓝色收纳盒' },
    { field: '数量', before: 2, after: 3 },
  ])
  assert.deepEqual(business, { product: '蓝色收纳袋', quantity: 2 })
})

test('applies the confirmed correction and returns an auditable change list', () => {
  const business = { product: '蓝色收纳袋', quantity: 2 }
  const applied = applySubtitleCorrection(business, { product: '蓝色收纳盒', quantity: 3 })
  assert.deepEqual(applied.business, { product: '蓝色收纳盒', quantity: 3 })
  assert.equal(applied.changes.length, 2)
})

test('no-op corrections stay explicit', () => {
  const business = { product: '蓝色收纳盒', quantity: 3 }
  const applied = applySubtitleCorrection(business, { product: '蓝色收纳盒', quantity: 3 })
  assert.deepEqual(applied.changes, [])
  assert.deepEqual(applied.business, business)
})