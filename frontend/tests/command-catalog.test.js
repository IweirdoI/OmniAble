import assert from 'node:assert/strict'
import test from 'node:test'
import { createCommandCatalog, createAnnouncer } from '../src/command-catalog.js'

const catalog = () => {
  const c = createCommandCatalog()
  c.register({ id: 'chat.new', name: '新建对话', aliases: ['新对话'], keywords: ['new'], help: '清空当前对话', run: async () => 'new' })
  c.register({ id: 'user.switch', name: '切换用户', aliases: ['用户'], help: '聚焦用户输入框', run: async () => 'user' })
  c.register({ id: 'sync.now', name: '同步记忆数据', aliases: ['同步'], keywords: ['sync'], help: '重新拉取数据', run: async () => 'sync' })
  c.register({ id: 'chat.send', name: '发送消息', aliases: ['发送'], enabled: () => false, run: async () => 'send' })
  return c
}

test('register validates required fields and rejects duplicate ids', () => {
  const c = createCommandCatalog()
  assert.throws(() => c.register({ id: 'x', name: 'X' }), /处理器/)
  assert.throws(() => c.register({ id: 'x', run: () => {} }), /名称/)
  c.register({ id: 'x', name: 'X', run: () => {} })
  assert.throws(() => c.register({ id: 'x', name: 'X', run: () => {} }), /重复/)
})

test('match: exact alias beats prefix beats substring', () => {
  const c = catalog()
  const exact = c.match('同步')
  assert.equal(exact[0].id, 'sync.now')
  const prefix = c.match('新建')
  assert.equal(prefix[0].id, 'chat.new')
  const keyword = c.match('new')
  assert.equal(keyword[0].id, 'chat.new')
})

test('empty query lists all enabled commands, disabled ones excluded', () => {
  const c = catalog()
  const ids = c.match('').map((item) => item.id)
  assert.ok(ids.includes('chat.new'))
  assert.ok(!ids.includes('chat.send'), 'enabled=false 的命令不进列表')
  const hit = c.match('发送')
  assert.deepEqual(hit.map((item) => item.id), [])
})

test('query normalizes case and whitespace', () => {
  const c = catalog()
  const ids = c.match('  NEW  ').map((item) => item.id)
  assert.equal(ids[0], 'chat.new')
})

test('get returns registered command or null', () => {
  const c = catalog()
  assert.equal(c.get('chat.new').name, '新建对话')
  assert.equal(c.get('missing'), null)
})

test('announcer dedupes identical text within the window only', () => {
  let t = 0
  const a = createAnnouncer({ dedupeMs: 500, now: () => t })
  assert.deepEqual(a.announce('已开始新对话'), { announced: true, dropped: 0 })
  assert.equal(a.announce('已开始新对话').announced, false)
  assert.equal(a.announce('已开始新对话').dropped, 2)
  t = 200
  assert.equal(a.announce('已开始新对话').announced, false, '窗口内仍去重')
  t = 501
  assert.deepEqual(a.announce('已开始新对话'), { announced: true, dropped: 0 })
  assert.equal(a.announce('另一条消息').announced, true, '不同文本立即播报')
})

test('announcer ignores empty text', () => {
  const a = createAnnouncer()
  assert.equal(a.announce('').announced, false)
  assert.equal(a.announce(null).announced, false)
})
