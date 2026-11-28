import test from 'node:test'
import assert from 'node:assert/strict'

import {
  ROLES, RUN_STATE, createDemoOrder, createDemoStock, createDemoSubstitutes,
  createStockSimulation, generateBOM,
} from '../src/stock-simulation.js'

const staff = { name: '现场-小魏', role: ROLES.STAFF }
const staff2 = { name: '现场-小赵', role: ROLES.STAFF }
const agent = { name: 'agent', role: ROLES.AGENT }

function fakeClock() {
  let t = 1000
  return { now: () => t, advance: ms => { t += ms } }
}

function demoSession(overrides = {}) {
  return createStockSimulation({
    order: createDemoOrder(),
    stock: createDemoStock(),
    substitutes: createDemoSubstitutes(),
    ...overrides,
  })
}

test('generates BOM with bins and positive quantities from a confirmed order', () => {
  const bom = generateBOM(createDemoOrder())
  assert.equal(bom.length, 3)
  assert.deepEqual(
    bom.map(item => [item.sku, item.requiredQty]),
    [['cup-yunwen', 2], ['box-blue', 3], ['frame-acrylic', 1]],
  )
  assert.equal(bom[0].bin, 'A1')
  assert.throws(() => generateBOM({ orderRef: 'X', items: [{ sku: 'a', name: 'a', qty: 0 }] }))
})

test('normal run completes without stock-out and decrements stock atomically', () => {
  const session = demoSession({
    stock: { 'cup-yunwen': 2, 'box-blue': 3, 'frame-acrylic': 1 },
  })
  assert.ok(session.startRun(staff).ok)
  let result = session.runNext(staff)
  assert.equal(result.picked.sku, 'cup-yunwen')
  result = session.runNext(staff)
  assert.equal(result.picked.sku, 'box-blue')
  result = session.runNext(staff)
  assert.equal(result.completed, true)
  assert.equal(session.snapshot().runState, RUN_STATE.COMPLETED)
  const stock = session.snapshot().stockLevels
  assert.deepEqual(stock, { 'cup-yunwen': 0, 'box-blue': 0, 'frame-acrylic': 0 })
  // 终态后重复执行被拒绝，不会产生第二次取放。
  assert.equal(session.runNext(staff).error, 'not_running')
})

test('stock-out stops the run in an identifiable state and keeps finished picks', () => {
  const session = demoSession()
  session.startRun(staff)
  const first = session.runNext(staff)
  assert.equal(first.picked.sku, 'cup-yunwen')

  const stockOut = session.runNext(staff)
  assert.equal(stockOut.stockOut, true)
  const snap = session.snapshot()
  assert.equal(snap.runState, RUN_STATE.AWAITING_DECISION)
  assert.equal(snap.pendingDecision.missingSku, 'box-blue')
  assert.equal(snap.pendingDecision.requiredQty, 3)
  assert.equal(snap.pendingDecision.available, 1)
  assert.equal(snap.done, 1)
  assert.equal(snap.total, 3)
  // 缺货期间库存未扣减。
  assert.equal(snap.stockLevels['box-blue'], 1)
})

test('substitution is human-only, validated, and resumes from the breakpoint', () => {
  const session = demoSession()
  session.startRun(staff)
  session.runNext(staff)
  session.runNext(staff) // box-blue 缺货

  // Agent 不能代替人做缺货决定。
  assert.equal(
    session.chooseSubstitution(agent, { alternativeSku: 'box-walnut' }).error,
    'decision_forbidden',
  )
  assert.equal(
    session.confirmDecision(agent, {}).error,
    'decision_forbidden',
  )
  // 未先选择就确认、未知替代、替代库存不足都被拒绝。
  assert.equal(session.confirmDecision(staff, {}).error, 'no_choice')
  assert.equal(
    session.chooseSubstitution(staff, { alternativeSku: 'nope' }).error,
    'unknown_alternative',
  )
  // cup-white 是另一件商品的替代项，不在 box-blue 的清单里。
  assert.equal(
    session.chooseSubstitution(staff, { alternativeSku: 'cup-white' }).error,
    'unknown_alternative',
  )
  const noStock = session.chooseSubstitution(staff, { alternativeSku: 'box-small' })
  assert.equal(noStock.error, 'insufficient_stock')

  const chosen = session.chooseSubstitution(staff, { alternativeSku: 'box-walnut' })
  assert.ok(chosen.ok)
  assert.equal(chosen.chosen.sku, 'box-walnut')

  const confirmed = session.confirmDecision(staff, {})
  assert.ok(confirmed.ok)
  assert.deepEqual(confirmed.substitution, { from: 'box-blue', to: 'box-walnut', qty: 3 })

  // 从断点继续：胡桃木收纳盒按需取 3，最后一件原子完成。
  const pick = session.runNext(staff)
  assert.equal(pick.picked.sku, 'box-walnut')
  const last = session.runNext(staff)
  assert.equal(last.completed, true)

  const snap = session.snapshot()
  assert.equal(snap.runState, RUN_STATE.COMPLETED)
  assert.deepEqual(snap.substitutions, [{ from: 'box-blue', to: 'box-walnut', qty: 3 }])
  assert.equal(snap.stockLevels['box-walnut'], 1)
  assert.equal(snap.done, 3)
})

test('duplicate confirmation and duplicate start are idempotent', () => {
  const session = demoSession()
  const started = session.startRun(staff, { idempotencyKey: 'retry-1' })
  const startedAgain = session.startRun(staff2, { idempotencyKey: 'retry-1' })
  assert.ok(started.ok && startedAgain.ok)
  assert.equal(startedAgain.duplicate, true)

  session.runNext(staff)
  session.runNext(staff) // 缺货
  session.chooseSubstitution(staff, { alternativeSku: 'box-walnut' })
  const first = session.confirmDecision(staff, {})
  const second = session.confirmDecision(staff, {})
  assert.equal(second.duplicate, true)
  assert.equal(session.snapshot().stockLevels['box-walnut'], 4)

  // 完成后的重复 resume 也不重跑。
  while (!session.runNext(staff).completed) { /* 依次取完 */ }
  const resume = session.resumeRun(staff)
  assert.equal(resume.error, 'not_resumable')
})

test('stale expected state version is rejected without changing anything', () => {
  const session = demoSession()
  const started = session.startRun(staff)
  const stale = started.stateVersion - 1
  assert.equal(session.runNext(staff, { expectedStateVersion: stale }).error, 'state_changed')
  session.runNext(staff)
  session.runNext(staff) // 缺货
  assert.equal(
    session.chooseSubstitution(staff, { alternativeSku: 'box-walnut', expectedStateVersion: stale }).error,
    'state_changed',
  )
  assert.equal(session.snapshot().pendingDecision.chosen, null)
  assert.equal(session.snapshot().stockLevels['box-blue'], 1)
})

test('agent link loss blocks agent but staff keeps working; reconnect resumes', () => {
  const clock = fakeClock()
  const session = demoSession({ now: clock.now })
  session.agentConnect(agent)
  session.startRun(agent)
  session.runNext(agent)
  session.runNext(agent) // 缺货

  session.agentDisconnect(agent, 'network_drop')
  assert.equal(session.snapshot().agentLinkLost, true)
  assert.equal(session.runNext(agent).error, 'link_lost')

  // 断联期间工作人员仍可选择并确认替代材料。
  assert.ok(session.chooseSubstitution(staff, { alternativeSku: 'box-walnut' }).ok)
  assert.ok(session.confirmDecision(staff, {}).ok)

  // 重连后 Agent 从断点继续，已完成物料不重复执行。
  assert.ok(session.agentConnect(agent).ok)
  const pick = session.runNext(agent)
  assert.equal(pick.picked.sku, 'box-walnut')
  assert.equal(session.snapshot().stockLevels['box-walnut'], 1)
})

test('heartbeat timeout freezes the agent link on a controllable clock', () => {
  const clock = fakeClock()
  const session = demoSession({ now: clock.now, linkTimeoutMs: 45000 })
  session.agentConnect(agent)
  session.startRun(agent)
  clock.advance(30000)
  session.agentHeartbeat(agent)
  clock.advance(40000)
  session.tick()
  assert.equal(session.snapshot().agentLinkLost, false)
  clock.advance(50000)
  session.tick()
  assert.equal(session.snapshot().agentLinkLost, true)
  assert.ok(session.events.some(item => item.kind === 'link_lost' && item.detail === 'heartbeat_timeout'))
})

test('unconfirmed decision times out but never auto-substitutes; staff can still confirm', () => {
  const clock = fakeClock()
  const session = demoSession({ now: clock.now, confirmTimeoutMs: 120000 })
  session.startRun(staff)
  session.runNext(staff)
  session.runNext(staff) // 缺货

  clock.advance(130000)
  session.tick()
  const snap = session.snapshot()
  assert.equal(snap.decisionTimedOut, true)
  assert.equal(snap.runState, RUN_STATE.AWAITING_DECISION)
  assert.equal(snap.pendingDecision.chosen, null)

  const late = session.runNext(staff)
  assert.equal(late.awaitingDecision, true)
  assert.ok(late.message.includes('不会自动选择替代材料'))

  // 超时只做标记；人来确认仍然有效，不会卡死流程。
  assert.ok(session.chooseSubstitution(staff, { alternativeSku: 'box-walnut' }).ok)
  assert.ok(session.confirmDecision(staff, {}).ok)
  assert.equal(session.snapshot().runState, RUN_STATE.RUNNING)
})

test('staff stop is identifiable; resume continues from breakpoint without re-picking', () => {
  const session = demoSession()
  session.startRun(staff)
  session.runNext(staff) // cup-yunwen 完成
  const stopped = session.stopRun(staff, { reason: 'break' })
  assert.ok(stopped.ok)
  assert.equal(stopped.done, 1)
  assert.equal(session.snapshot().runState, RUN_STATE.STOPPED)

  assert.equal(session.stopRun(staff2, {}).error, 'not_running')
  const resumed = session.resumeRun(staff)
  assert.ok(resumed.ok)
  assert.equal(resumed.remaining, 2)

  const stockOut = session.runNext(staff)
  assert.equal(stockOut.stockOut, true)
  assert.ok(session.cancelRun(staff, { reason: 'shift_end' }).ok)
  assert.equal(session.snapshot().runState, RUN_STATE.CANCELLED)
  assert.equal(session.runNext(staff).error, 'not_running')
})
