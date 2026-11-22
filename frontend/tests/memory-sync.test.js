import assert from 'node:assert/strict'
import test from 'node:test'
import { createMemorySync } from '../src/memory-sync.js'

test('initial total failure never publishes zero counts or a success timestamp', async () => {
  const panels = {}
  const sync = createMemorySync(panels, async () => { throw new Error('offline') }, () => '10:00')
  await sync.refresh('alice')
  for (const panel of Object.values(panels)) {
    assert.deepEqual(panel, { status: 'error', updatedAt: null, data: null })
  }
})

test('partial failure retains the old timestamp and single retry updates only that panel', async () => {
  const panels = {}
  const calls = []
  let version = 1
  const sync = createMemorySync(panels, async (key, user) => {
    calls.push([key, user])
    if (version === 2 && key === 'profile') throw new Error('unavailable')
    return `${key}-${version}`
  }, () => `10:0${version}`)
  await sync.refresh('alice')
  version = 2
  await sync.refresh('alice')
  assert.deepEqual(panels.profile, { status: 'stale', updatedAt: '10:01', data: 'profile-1' })
  assert.equal(panels.stats.updatedAt, '10:02')
  calls.length = 0
  version = 3
  await sync.refresh('alice', ['profile'])
  assert.deepEqual(calls, [['profile', 'alice']])
  assert.deepEqual(panels.profile, { status: 'success', updatedAt: '10:03', data: 'profile-3' })
  assert.equal(panels.stats.updatedAt, '10:02')
})

test('reset after a user switch discards late responses from the previous user', async () => {
  const panels = {}
  let release
  const previous = new Promise(resolve => { release = resolve })
  const sync = createMemorySync(panels, (_, user) => user === 'alice' ? previous : 'bob-profile', () => '10:02')
  const pending = sync.refresh('alice', ['profile'])
  sync.reset()
  await sync.refresh('bob', ['profile'])
  release('alice-profile')
  await pending
  assert.equal(panels.profile.data, 'bob-profile')
})

test('refresh failure after a confirmed forget cannot show the old preference', async () => {
  const panels = {}
  let forgotten = false
  const sync = createMemorySync(panels, async () => {
    if (forgotten) throw new Error('temporarily unavailable')
    return ['old preference']
  }, () => '10:02')
  await sync.refresh('alice', ['profile', 'history'])
  forgotten = true
  sync.reset()
  await sync.refresh('alice', ['profile', 'history'])
  for (const key of ['profile', 'history']) {
    assert.deepEqual(panels[key], { status: 'error', updatedAt: null, data: null })
  }
})
