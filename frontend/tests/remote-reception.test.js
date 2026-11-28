import test from 'node:test'
import assert from 'node:assert/strict'

import {
  CONTROL_STATE, ROLES, createRemoteSession, createShowcaseCatalog,
} from '../src/remote-reception.js'

const remote = { name: '远程-小林', role: ROLES.REMOTE_STAFF }
const remote2 = { name: '远程-小陈', role: ROLES.REMOTE_STAFF }
const site = { name: '现场-小魏', role: ROLES.SITE_ASSISTANT }

// 可控时钟：heartbeat 超时冻结用。
function fakeClock() {
  let t = 1000
  return { now: () => t, advance: ms => { t += ms } }
}

test('fictional catalog provides three synthetic products with spec versions', () => {
  const catalog = createShowcaseCatalog()
  assert.equal(catalog.length, 3)
  for (const item of catalog) {
    assert(item.key && item.name && item.description)
    assert(Number.isInteger(item.specVersion) && item.specVersion >= 1)
  }
})

test('observation is read-only, requires grant, and degrades to text description', () => {
  const session = createRemoteSession()
  assert.equal(session.observeProduct(remote, 'cup-yunwen').error, 'observe_required')

  assert.ok(session.grantObserve(remote).ok)
  const snapshot = session.observeProduct(remote, 'cup-yunwen')
  assert.ok(snapshot.ok)
  assert.equal(snapshot.snapshot.onDisplay, true)
  assert.equal(snapshot.snapshot.name, '云纹陶瓷杯')
  assert.ok(snapshot.snapshot.description.includes('青白釉陶瓷杯'))
  assert.equal(snapshot.snapshot.deviceStateVersion, 1)

  // 未在展示位的商品仍有文字描述（观察流不依赖视频可用）。
  assert.ok(session.observeProduct(remote, 'box-walnut').snapshot.description.includes('未在转台展示位'))

  // 观察不改变设备状态，也不需要控制权；撤销权限由现场协助者管理。
  assert.equal(session.snapshot().device.stateVersion, 1)
  assert.equal(session.revokeObserve(remote, remote.name).error, 'role_forbidden')
  assert.ok(session.revokeObserve(site, remote.name).ok)
  assert.equal(session.observeProduct(remote, 'cup-yunwen').error, 'observe_required')
})

test('spec query flags stale version; confirming a stale draft is blocked with recheck', () => {
  const session = createRemoteSession()
  session.grantObserve(remote)

  const stale = session.querySpec(remote, 'cup-yunwen', { expectedSpecVersion: 1 })
  assert.equal(stale.stale, true)
  assert.ok(stale.message.includes('复核'))

  const fresh = session.querySpec(remote, 'cup-yunwen')
  assert.equal(fresh.stale, false)
  assert.equal(fresh.specVersion, 2)

  const draft = session.draftInquiry(remote, {
    productKey: 'cup-yunwen', customerAsk: '这款杯子的容量是多少？', answer: '350 毫升。',
  })
  assert.ok(draft.ok)
  assert.equal(draft.draft.specVersion, 2)

  // 规格更新到 v3 后，基于 v2 的草稿必须重新起草复核。
  assert.equal(session.updateSpec(remote2, 'cup-yunwen', { 材质: '陶瓷', 容量: '400 毫升' }).error, 'edit_forbidden')
  const bumped = session.updateSpec(site, 'cup-yunwen', { 材质: '陶瓷', 容量: '400 毫升', 颜色: '青白' })
  assert.ok(bumped.ok)
  assert.equal(bumped.specVersion, 3)

  const rejected = session.confirmInquiry(remote, draft.draft.draftId)
  assert.equal(rejected.error, 'source_updated')
  assert.ok(rejected.message.includes('重新起草'))

  const redraft = session.draftInquiry(remote, {
    productKey: 'cup-yunwen', customerAsk: '这款杯子的容量是多少？', answer: '400 毫升。',
  })
  assert.equal(redraft.draft.specVersion, 3)
  const confirmed = session.confirmInquiry(remote, redraft.draft.draftId)
  assert.ok(confirmed.ok)
  assert.equal(session.listInquiries().inquiries.length, 1)
})

test('inquiry confirmation is idempotent: duplicate confirm returns the same record', () => {
  const session = createRemoteSession()
  const draft = session.draftInquiry(remote, {
    productKey: 'box-walnut', customerAsk: '可以订 10 个吗？', answer: '可以，按单价 68 元。',
  })
  const first = session.confirmInquiry(remote, draft.draft.draftId)
  const second = session.confirmInquiry(remote, draft.draft.draftId)
  assert.ok(first.ok && second.ok)
  assert.equal(second.duplicate, true)
  assert.deepEqual(second.record, first.record)
  assert.equal(session.listInquiries().inquiries.length, 1)
})

test('control is exclusive; site takeover voids pending remote device requests', () => {
  const session = createRemoteSession()
  assert.ok(session.requestControl(remote).ok)
  assert.equal(session.snapshot().controlState, CONTROL_STATE.REMOTE)

  assert.equal(session.requestControl(remote2).error, 'control_busy')
  assert.equal(session.requestDeviceAction(remote2, {
    action: 'switch_display', targetKey: 'box-walnut', expectedStateVersion: 1,
  }).error, 'control_required')

  const pending = session.requestDeviceAction(remote, {
    action: 'switch_display', targetKey: 'box-walnut', expectedStateVersion: 1,
  })
  assert.ok(pending.ok)

  const takeover = session.takeoverBySite(site)
  assert.ok(takeover.ok)
  assert.equal(session.snapshot().controlState, CONTROL_STATE.SITE)
  assert.equal(takeover.voided.length, 1)
  assert.equal(session.snapshot().pendingConfirmations.length, 0)

  // 被现场接管后，远程设备动作被拒，且设备未变化。
  assert.equal(session.requestDeviceAction(remote, {
    action: 'switch_display', targetKey: 'box-walnut', expectedStateVersion: 1,
  }).error, 'control_required')
  assert.equal(session.snapshot().device.displaySlot, 1)
})

test('device action follows the confirmation chain: token, CAS, deny and duplicate', () => {
  const session = createRemoteSession()
  // 执行前拒绝链：未知动作最先拒绝，未知目标随后（不需要控制权即可区分动作合法性）。
  assert.equal(session.requestDeviceAction(remote, {
    action: 'unknown_arm', targetKey: 'box-walnut', expectedStateVersion: 1,
  }).error, 'unknown_action')

  session.requestControl(remote)
  assert.equal(session.requestDeviceAction(remote, {
    action: 'switch_display', targetKey: 'nope', expectedStateVersion: 1,
  }).error, 'unknown_target')

  const request = session.requestDeviceAction(remote, {
    action: 'switch_display', targetKey: 'box-walnut', expectedStateVersion: 1,
  })
  const { confirmId } = request.confirmation
  assert.ok(request.confirmation.summary.includes('展示转台'))

  assert.equal(
    session.decideDeviceAction(remote, confirmId, { decision: 'approve', token: 'wrong' }).error,
    'bad_token',
  )

  const denied = session.decideDeviceAction(remote, confirmId, { decision: 'deny', token: request.decisionToken })
  assert.equal(denied.status, 'denied')
  assert.equal(session.snapshot().device.displaySlot, 1)
  assert.equal(session.decideDeviceAction(remote, confirmId, { decision: 'approve', token: request.decisionToken }).duplicate, true)

  const second = session.requestDeviceAction(remote, {
    action: 'switch_display', targetKey: 'box-walnut', expectedStateVersion: 1,
  })
  const approved = session.decideDeviceAction(remote, second.confirmation.confirmId, {
    decision: 'approve', token: second.decisionToken,
  })
  assert.equal(approved.status, 'executed')
  assert.equal(session.snapshot().device.displaySlot, 2)
  assert.equal(session.snapshot().device.stateVersion, 2)

  // 携带过期期望版本的动作在确认时被 CAS 拒绝。
  const third = session.requestDeviceAction(remote, {
    action: 'switch_display', targetKey: 'frame-acrylic', expectedStateVersion: 1,
  })
  const stale = session.decideDeviceAction(remote, third.confirmation.confirmId, {
    decision: 'approve', token: third.decisionToken,
  })
  assert.equal(stale.error, 'revision_changed')
  assert.equal(session.snapshot().device.displaySlot, 2)
})

test('disconnect freezes remote side; site keeps working; reconnect does not auto-resend', () => {
  const session = createRemoteSession()
  session.grantObserve(remote)
  session.remoteConnect(remote)
  session.requestControl(remote)
  const pending = session.requestDeviceAction(remote, {
    action: 'move_sample', targetKey: 'frame-acrylic', expectedStateVersion: 1,
  })
  assert.ok(pending.ok)

  assert.ok(session.remoteDisconnect(remote, 'remote_link_lost').ok)
  // 断联只冻结远程链路：远程侧被拒，控制权随远程持有者一起释放。
  assert.equal(session.snapshot().linkFrozen, true)
  assert.equal(session.snapshot().controlState, CONTROL_STATE.IDLE)
  assert.equal(session.observeProduct(remote, 'cup-yunwen').error, 'link_frozen')
  assert.equal(session.requestDeviceAction(remote, {
    action: 'switch_display', targetKey: 'box-walnut', expectedStateVersion: 1,
  }).error, 'link_frozen')

  // 断联期间现场协助者仍可观察并接管。
  assert.ok(session.grantObserve(site).ok)
  assert.ok(session.observeProduct(site, 'frame-acrylic').ok)
  const takeover = session.takeoverBySite(site)
  assert.ok(takeover.ok)
  assert.equal(session.snapshot().controlState, CONTROL_STATE.SITE)

  const reconnected = session.reconnectRemote(remote)
  assert.ok(reconnected.ok)
  assert.equal(reconnected.voided.length, 1)
  assert.equal(reconnected.voided[0].action, 'move_sample')
  assert.equal(session.snapshot().linkFrozen, false)
  // 现场接管取得控制权，重连后控制权仍在现场。
  assert.equal(session.snapshot().controlState, CONTROL_STATE.SITE)
  assert.equal(session.requestControl(remote).error, 'control_busy')

  // 现场释放后远程才能重新请求；作废请求不自动重发。
  assert.ok(session.releaseControl(site).ok)
  assert.ok(session.requestControl(remote).ok)
  assert.equal(session.snapshot().pendingConfirmations.length, 0)
  assert.equal(session.snapshot().device.displaySlot, 1)
})

test('heartbeat timeout freezes the remote link on a controllable clock', () => {
  const clock = fakeClock()
  const session = createRemoteSession({ now: clock.now, linkTimeoutMs: 45000 })
  session.remoteConnect(remote)

  clock.advance(30000)
  session.remoteHeartbeat(remote)
  clock.advance(40000)
  session.tick()
  assert.equal(session.snapshot().linkFrozen, false)

  clock.advance(50000)
  session.tick()
  assert.equal(session.snapshot().linkFrozen, true)
  assert.equal(session.snapshot().controlState, CONTROL_STATE.IDLE)
  assert.ok(session.events.some(item => item.kind === 'link_frozen' && item.detail === 'heartbeat_timeout'))
})
