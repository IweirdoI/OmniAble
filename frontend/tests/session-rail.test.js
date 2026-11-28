import assert from 'node:assert/strict'
import test from 'node:test'
import { createSessionRail } from '../src/session-rail.js'

const state = () => ({ status: 'idle', items: [], total: 0, error: null, updatedAt: null })

test('load populates the list and scopes the request path to the user', async () => {
  const s = state()
  const paths = []
  const rail = createSessionRail(s, async (path) => {
    paths.push(path)
    return { total: 1, offset: 0, limit: 50, items: [{ id: 'a', title: 'T', message_count: 2 }] }
  }, () => '10:00')
  assert.equal(await rail.load('alice'), true)
  assert.equal(paths[0], '/agent/sessions/alice?offset=0&limit=50')
  assert.equal(s.status, 'ready')
  assert.equal(s.items[0].id, 'a')
  assert.equal(s.updatedAt, '10:00')
  await rail.load('bob')
  assert.ok(paths[1].startsWith('/agent/sessions/bob?'), '切换用户后请求路径必须换用户')
})

test('initial load failure reports error instead of faking an empty list', async () => {
  const s = state()
  const rail = createSessionRail(s, async () => { throw new Error('offline') })
  assert.equal(await rail.load('alice'), false)
  assert.equal(s.status, 'error')
  assert.equal(s.error, 'offline')
  assert.deepEqual(s.items, [])
})

test('failed refresh keeps stale items and marks the list stale', async () => {
  const s = state()
  let fail = false
  const rail = createSessionRail(s, async () => {
    if (fail) throw new Error('down')
    return { total: 1, offset: 0, limit: 50, items: [{ id: 'a', title: 'T', message_count: 1 }] }
  })
  await rail.load('alice')
  fail = true
  assert.equal(await rail.load('alice'), false)
  assert.equal(s.status, 'stale')
  assert.equal(s.items.length, 1)
})

test('an older list response is discarded once a newer load started', async () => {
  const s = state()
  let release
  const gate = new Promise((resolve) => { release = resolve })
  let call = 0
  const rail = createSessionRail(s, async () => {
    call += 1
    if (call === 1) {
      await gate
      return { total: 1, offset: 0, limit: 50, items: [{ id: 'old', title: 'Old', message_count: 1 }] }
    }
    return { total: 0, offset: 0, limit: 50, items: [] }
  })
  const first = rail.load('alice')
  const second = rail.load('alice')
  release()
  assert.equal(await first, false, '过期响应不得写入状态')
  assert.equal(await second, true)
  assert.deepEqual(s.items, [])
})

test('load more appends pages without clobbering earlier items', async () => {
  const s = state()
  const rail = createSessionRail(s, async (path) => {
    const offset = Number(new URLSearchParams(path.split('?')[1]).get('offset'))
    return offset === 0
      ? { total: 2, offset: 0, limit: 1, items: [{ id: 'a', title: 'A', message_count: 1 }] }
      : { total: 2, offset: 1, limit: 1, items: [{ id: 'b', title: 'B', message_count: 1 }] }
  })
  await rail.load('alice', { limit: 1 })
  assert.equal(await rail.load('alice', { offset: s.items.length, limit: 1 }), true)
  assert.deepEqual(s.items.map((item) => item.id), ['a', 'b'])
  assert.equal(s.status, 'ready')
})

test('create posts the user and title and returns the session', async () => {
  const s = state()
  let captured = null
  const rail = createSessionRail(s, async (path, options) => {
    captured = { path, options }
    return { user_id: 'alice', session: { id: 's1', title: 'T' } }
  })
  const session = await rail.create('alice', '标题')
  assert.equal(captured.path, '/agent/sessions')
  assert.equal(captured.options.method, 'POST')
  assert.deepEqual(JSON.parse(captured.options.body), { user_id: 'alice', title: '标题' })
  assert.equal(session.id, 's1')
})

test('open maps stored messages and keeps the suppressed placeholder visible', async () => {
  const rail = createSessionRail(state(), async (path) => {
    assert.equal(path, '/agent/sessions/alice/s1')
    return { user_id: 'alice', session: { id: 's1', title: 'T', messages: [
      { id: 1, role: 'user', content: 'hi', created_at: '2026-01-01T10:05:00', suppressed: 0 },
      { id: 2, role: 'assistant', content: '[该条消息已按遗忘指令删除]', created_at: '2026-01-01T10:06:00', suppressed: 1 },
    ] } }
  })
  const detail = await rail.open('alice', 's1')
  assert.equal(detail.session.id, 's1')
  assert.equal(detail.messages[0].role, 'user')
  assert.equal(detail.messages[0].restored, true)
  assert.equal(detail.messages[0].suppressed, false)
  assert.equal(detail.messages[1].suppressed, true)
  assert.match(detail.messages[1].content, /已按遗忘指令删除/)
  assert.match(detail.messages[0].time, /10:05/)
})

test('a superseded open resolves null and must not backfill the chat', async () => {
  let release
  const gate = new Promise((resolve) => { release = resolve })
  let call = 0
  const rail = createSessionRail(state(), async () => {
    call += 1
    if (call === 1) {
      await gate
      return { session: { id: 'old', messages: [{ id: 1, role: 'user', content: 'old', created_at: '', suppressed: 0 }] } }
    }
    return { session: { id: 'new', messages: [] } }
  })
  const first = rail.open('alice', 'old')
  const second = rail.open('bob', 'new')
  release()
  assert.equal(await first, null, '被更新打开取代的详情不得回填')
  assert.equal((await second).session.id, 'new')
})

test('open failure propagates so the caller can report it honestly', async () => {
  const rail = createSessionRail(state(), async () => { throw new Error('404') })
  await assert.rejects(() => rail.open('alice', 'missing'), /404/)
})

test('remove issues a DELETE scoped to the user and session', async () => {
  let captured = null
  const rail = createSessionRail(state(), async (path, options) => {
    captured = { path, options }
    return { user_id: 'alice', session_id: 's1', deleted: true }
  })
  assert.equal(await rail.remove('alice', 's1'), true)
  assert.equal(captured.path, '/agent/sessions/alice/s1')
  assert.equal(captured.options.method, 'DELETE')
})

test('reset clears state and invalidates an in-flight load', async () => {
  const s = state()
  let release
  const gate = new Promise((resolve) => { release = resolve })
  const rail = createSessionRail(s, async () => {
    await gate
    return { total: 1, offset: 0, limit: 50, items: [{ id: 'a', title: 'T', message_count: 1 }] }
  })
  const pending = rail.load('alice')
  rail.reset()
  release()
  assert.equal(await pending, false, 'reset 后在途响应不得写入')
  assert.deepEqual(s.items, [])
  assert.equal(s.status, 'idle')
})
