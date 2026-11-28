// 备货模拟原型（TASK-046 / Issue #66）
// 从获确认的定制订单生成物料清单 → 模拟预设轻小物品取放设备逐项备货 → 注入一次缺货 →
// 停在可识别状态 → 工作人员选择替代材料并再次确认 → 从断点继续且不重复执行。
// 纯逻辑模拟，不接真机；设备验收契约、与真机的报告边界见 docs/STOCK_SIMULATION.md。
// 语义对齐：变更操作带期望状态版本（src/agent/workspace.py 乐观锁）、重复请求幂等且
// 已完成步骤跳过（src/agent/run_store.py）、人未确认的高影响决定不执行（执行确认门控）。
// 两个正交状态：agentLinkLost 只管远程链路（本地工作人员不受限）；runState 只反映设备运行状态。

export const ROLES = {
  STAFF: 'staff',   // 工作人员：本地处置，缺货替代与停止的决定权在人
  AGENT: 'agent',   // Agent：驱动取放流程，不能代替人做缺货决定
}

export const RUN_STATE = {
  IDLE: 'idle',                          // 未开始
  RUNNING: 'running',                    // 备货进行中
  AWAITING_DECISION: 'awaiting_decision', // 缺货待人工决定（可识别状态）
  STOPPED: 'stopped',                    // 工作人员停止
  CANCELLED: 'cancelled',                // 工作人员取消（终态）
  COMPLETED: 'completed',                // 全部物料完成（终态）
}

export const LINK_TIMEOUT_MS = 45000
export const CONFIRM_TIMEOUT_MS = 120000

const fail = (error, message) => ({ ok: false, error, message })
const isStaff = actor => actor?.role === ROLES.STAFF

// 虚构获确认订单与库存，全部合成数据。box-blue 库存不足以注入一次缺货。
export function createDemoOrder() {
  return {
    orderRef: 'SO-DEMO-01',
    items: [
      { sku: 'cup-yunwen', name: '云纹陶瓷杯', qty: 2 },
      { sku: 'box-blue', name: '蓝色收纳盒', qty: 3 },
      { sku: 'frame-acrylic', name: '亚克力相框', qty: 1 },
    ],
  }
}

export function createDemoStock() {
  return {
    'cup-yunwen': 2, 'box-blue': 1, 'frame-acrylic': 5,
    'box-walnut': 4, 'cup-white': 0, 'box-small': 0,
  }
}

export function createDemoSubstitutes() {
  // 预设替代材料：键为缺货 SKU，值为可选替代列表（含名称）；清单本身可含库存为 0 的项。
  return {
    'box-blue': [
      { sku: 'box-walnut', name: '胡桃木收纳盒' },
      { sku: 'box-small', name: '小号布艺收纳盒' },
    ],
    'cup-yunwen': [{ sku: 'cup-white', name: '白瓷杯' }],
  }
}

const BINS = {
  'cup-yunwen': 'A1', 'box-blue': 'A2', 'frame-acrylic': 'B1',
  'box-walnut': 'A3', 'cup-white': 'A4', 'box-small': 'A5',
}

// 物料清单：每件商品一行，含料箱位与所需数量；库存校验由模拟设备逐步执行。
export function generateBOM(order) {
  if (!order?.orderRef || !Array.isArray(order.items) || !order.items.length) {
    throw new Error('物料清单需要获确认的订单编号和至少一件商品。')
  }
  return order.items.map(item => {
    const qty = Number(item.qty)
    if (!Number.isInteger(qty) || qty <= 0) {
      throw new Error(`商品 ${item.sku} 的数量必须是正整数。`)
    }
    return { sku: item.sku, name: item.name, requiredQty: qty, bin: BINS[item.sku] || '?', status: 'pending' }
  })
}

export function createStockSimulation({
  order = createDemoOrder(),
  stock = createDemoStock(),
  substitutes = createDemoSubstitutes(),
  now = () => Date.now(),
  linkTimeoutMs = LINK_TIMEOUT_MS,
  confirmTimeoutMs = CONFIRM_TIMEOUT_MS,
} = {}) {
  const levels = { ...stock }
  const bom = generateBOM(order)
  let runState = RUN_STATE.IDLE
  let stateVersion = 1
  let agentLinkLost = false
  let decisionTimedOut = false
  let runKey = null
  let link = { active: false, lastBeat: 0 }
  let decision = null          // 缺货决定卡：{ missingSku, requiredQty, available, alternatives, chosen, decidedAt, confirmedAt }
  let decisionAwaitedAt = 0
  const events = []

  const log = (kind, detail) => events.push({ kind, detail, at: now() })
  const itemAt = index => bom[index]
  const nextIndex = () => bom.findIndex(item => item.status === 'pending' || item.status === 'replaced')
  const doneCount = () => bom.filter(item => item.status === 'done').length

  function tick() {
    if (!agentLinkLost && link.active && now() - link.lastBeat > linkTimeoutMs) {
      agentLinkLost = true
      log('link_lost', 'heartbeat_timeout')
    }
    if (runState === RUN_STATE.AWAITING_DECISION && !decisionTimedOut &&
        now() - decisionAwaitedAt > confirmTimeoutMs) {
      decisionTimedOut = true
      log('decision_timeout', decision?.missingSku)
    }
  }

  function refreshBeat(actor) {
    if (actor?.role === ROLES.AGENT) link = { active: true, lastBeat: now() }
  }

  const agentBlocked = () => agentLinkLost
    ? fail('link_lost', 'Agent 远程链路已中断；本地工作人员不受影响，可继续操作。')
    : null

  // 变更操作的乐观锁：调用方携带期望状态版本时，不匹配即拒绝且不变更。
  const casOk = expected => expected == null || Number(expected) === stateVersion
  const casFail = () => fail('state_changed', '设备状态已变化；请重新读取快照后再次发起。')

  const runFinished = () => runState === RUN_STATE.COMPLETED || runState === RUN_STATE.CANCELLED

  return {
    tick,

    agentConnect(actor) {
      if (actor?.role !== ROLES.AGENT) return fail('role_forbidden', '只有 Agent 需要建立远程链路。')
      agentLinkLost = false
      link = { active: true, lastBeat: now() }
      log('agent_connected', actor.name || 'agent')
      return { ok: true, message: 'Agent 链路已建立。' }
    },

    agentHeartbeat(actor) {
      if (actor?.role !== ROLES.AGENT) return fail('role_forbidden', '只有 Agent 发送心跳。')
      refreshBeat(actor)
      return { ok: true }
    },

    agentDisconnect(actor, reason = 'agent_quit') {
      if (actor?.role !== ROLES.AGENT) return fail('role_forbidden', '只有 Agent 可以断开链路。')
      agentLinkLost = true
      log('link_lost', reason)
      return { ok: true, message: 'Agent 链路已断开；备货停在当前状态，不会自动重试。' }
    },

    // 幂等启动：同一 runKey 重复启动返回首次运行，不重复生成清单或执行。
    startRun(actor, { idempotencyKey, expectedStateVersion } = {}) {
      if (!casOk(expectedStateVersion)) return casFail()
      if (runFinished() || runState !== RUN_STATE.IDLE) {
        return { ok: true, duplicate: true, message: '运行已存在或已结束；重复启动不产生第二次执行。' }
      }
      runKey = idempotencyKey || `run-${now()}`
      runState = RUN_STATE.RUNNING
      stateVersion += 1
      log('run_started', runKey)
      return { ok: true, runKey, stateVersion, message: `备货运行 ${runKey} 已启动；重复启动不会重复执行。` }
    },

    // 执行下一步取放：库存足够则完成该物料；不足则停下转入待人工决定。
    runNext(actor, { expectedStateVersion } = {}) {
      tick()
      if (actor?.role === ROLES.AGENT) {
        refreshBeat(actor)
        const blocked = agentBlocked()
        if (blocked) return blocked
      }
      if (!casOk(expectedStateVersion)) return casFail()
      if (runState === RUN_STATE.AWAITING_DECISION) {
        return {
          ok: true, awaitingDecision: true,
          decision: { missingSku: decision.missingSku, alternatives: decision.alternatives },
          message: decisionTimedOut
            ? '缺货决定仍在等待人工确认（已超时标记）；不会自动选择替代材料。'
            : '缺货待人工决定；Agent 不能代替人选择替代材料。',
        }
      }
      if (runState !== RUN_STATE.RUNNING) {
        return fail('not_running', `设备当前状态为 ${runState}，不在备货中。`)
      }
      const index = nextIndex()
      if (index === -1) {
        runState = RUN_STATE.COMPLETED
        stateVersion += 1
        log('run_completed', runKey)
        return { ok: true, completed: true, message: '全部物料备货完成。' }
      }
      const item = itemAt(index)
      const available = levels[item.sku] || 0
      if (available < item.requiredQty) {
        runState = RUN_STATE.AWAITING_DECISION
        decisionAwaitedAt = now()
        decisionTimedOut = false
        decision = {
          missingSku: item.sku, missingName: item.name, requiredQty: item.requiredQty,
          available,
          alternatives: (substitutes[item.sku] || [])
            .map(alt => ({ ...alt, stock: levels[alt.sku] || 0 })),
          chosen: null, decidedAt: null, confirmedAt: null,
        }
        stateVersion += 1
        log('stock_out', `${item.sku} 需要 ${item.requiredQty}，库存 ${available}`)
        return {
          ok: true, stockOut: true,
          decision: { missingSku: item.sku, missingName: item.name, requiredQty: item.requiredQty, available },
          message: `${item.name} 库存不足（需 ${item.requiredQty}，存 ${available}）；已停止备货，等待工作人员选择替代材料。`,
        }
      }
      levels[item.sku] = available - item.requiredQty
      item.status = 'done'
      item.pickedQty = item.requiredQty
      // 最后一件物料完成即原子进入 COMPLETED；重复 runNext 由状态机拒绝，不产生第二次执行。
      const finished = doneCount() === bom.length
      if (finished) runState = RUN_STATE.COMPLETED
      stateVersion += 1
      log(finished ? 'run_completed' : 'item_picked',
        finished ? runKey : `${item.sku} x${item.requiredQty} @${item.bin}`)
      return {
        ok: true, completed: finished,
        picked: { sku: item.sku, name: item.name, qty: item.requiredQty, bin: item.bin },
        remaining: bom.length - doneCount(),
        message: finished
          ? '全部物料备货完成。'
          : `已从 ${item.bin} 取出 ${item.name} x${item.requiredQty}。`,
      }
    },

    // 缺货接管第一步：工作人员选择替代材料（仅人可做；Agent 拒绝）。
    chooseSubstitution(actor, { alternativeSku, expectedStateVersion } = {}) {
      if (!isStaff(actor)) {
        return fail('decision_forbidden', '缺货替代由工作人员决定；Agent 不能代替人选择。')
      }
      if (!casOk(expectedStateVersion)) return casFail()
      if (runState !== RUN_STATE.AWAITING_DECISION || !decision) {
        return fail('not_awaiting', '当前没有等待决定的缺货事项。')
      }
      const alternative = decision.alternatives.find(alt => alt.sku === alternativeSku)
      if (!alternative) {
        return fail('unknown_alternative', `替代材料 ${alternativeSku} 不在预设清单里。`)
      }
      if ((levels[alternative.sku] || 0) < decision.requiredQty) {
        return fail('insufficient_stock', `替代材料 ${alternative.name} 库存也不足。`)
      }
      decision.chosen = alternative
      decision.decidedAt = now()
      log('substitution_chosen', `${decision.missingSku} -> ${alternative.sku}`)
      return { ok: true, chosen: alternative, message: `已选择 ${alternative.name}；确认后才会写入清单并继续备货。` }
    },

    // 缺货接管第二步：再次确认后才生效；重复确认返回同一结果。
    confirmDecision(actor, { expectedStateVersion } = {}) {
      if (!isStaff(actor)) {
        return fail('decision_forbidden', '缺货替代由工作人员确认；Agent 不能代替人确认。')
      }
      if (!casOk(expectedStateVersion)) return casFail()
      if (!decision) {
        return fail('not_awaiting', '当前没有等待确认的缺货事项。')
      }
      // 幂等先于状态守卫：确认生效后状态已前进，重复确认仍返回首次结果。
      if (decision.confirmedAt) {
        return { ok: true, duplicate: true, message: '此决定已确认过；重复确认不会产生第二次变更。' }
      }
      if (runState !== RUN_STATE.AWAITING_DECISION) {
        return fail('not_awaiting', '当前没有等待确认的缺货事项。')
      }
      if (!decision.chosen) {
        return fail('no_choice', '请先选择替代材料再确认。')
      }
      const missingIndex = bom.findIndex(item =>
        (item.status === 'pending' || item.status === 'replaced') && item.sku === decision.missingSku)
      if (missingIndex === -1) {
        return fail('unknown_target', `清单里没有待备货的 ${decision.missingSku}。`)
      }
      const original = bom[missingIndex]
      decision.confirmedAt = now()
      bom[missingIndex] = {
        sku: decision.chosen.sku, name: decision.chosen.name,
        requiredQty: original.requiredQty, bin: BINS[decision.chosen.sku] || '?',
        substitutedFor: decision.missingSku, status: 'pending',
      }
      runState = RUN_STATE.RUNNING
      stateVersion += 1
      log('substitution_confirmed', `${decision.missingSku} -> ${decision.chosen.sku}`)
      return {
        ok: true, stateVersion,
        substitution: { from: decision.missingSku, to: decision.chosen.sku, qty: original.requiredQty },
        message: `已确认用 ${decision.chosen.name} 替代 ${decision.missingName}；备货从断点继续，已完成物料不重复执行。`,
      }
    },

    // 工作人员停止：停在可识别状态；恢复时跳过已完成物料。
    stopRun(actor, { reason, expectedStateVersion } = {}) {
      if (!isStaff(actor)) return fail('role_forbidden', '停止备货由工作人员操作。')
      if (!casOk(expectedStateVersion)) return casFail()
      if (runFinished() || runState === RUN_STATE.IDLE || runState === RUN_STATE.STOPPED) {
        return fail('not_running', '设备当前不在备货中。')
      }
      runState = RUN_STATE.STOPPED
      stateVersion += 1
      log('run_stopped', reason || 'staff_stop')
      return {
        ok: true, stateVersion, done: doneCount(), remaining: bom.length - doneCount(),
        message: `备货已停止：已完成 ${doneCount()} 项；恢复时从断点继续，不重复执行。`,
      }
    },

    resumeRun(actor, { expectedStateVersion } = {}) {
      if (actor?.role === ROLES.AGENT) {
        refreshBeat(actor)
        const blocked = agentBlocked()
        if (blocked) return blocked
      }
      if (!casOk(expectedStateVersion)) return casFail()
      if (runState !== RUN_STATE.STOPPED && runState !== RUN_STATE.AWAITING_DECISION) {
        return fail('not_resumable', `设备当前状态为 ${runState}，无可恢复的停止点。`)
      }
      if (runState === RUN_STATE.AWAITING_DECISION) {
        return { ok: true, awaitingDecision: true, message: '缺货决定尚未确认；请先由工作人员选择替代材料并确认。' }
      }
      runState = RUN_STATE.RUNNING
      stateVersion += 1
      log('run_resumed', runKey)
      return { ok: true, stateVersion, remaining: bom.length - doneCount(), message: '备货已从断点恢复。' }
    },

    cancelRun(actor, { reason } = {}) {
      if (!isStaff(actor)) return fail('role_forbidden', '取消备货由工作人员操作。')
      if (runFinished()) return fail('already_finished', '运行已结束，无需取消。')
      runState = RUN_STATE.CANCELLED
      stateVersion += 1
      log('run_cancelled', reason || 'staff_cancel')
      return { ok: true, message: '备货已取消；已完成物料保留记录。' }
    },

    snapshot() {
      return {
        orderRef: order.orderRef,
        runKey,
        runState,
        stateVersion,
        agentLinkLost,
        decisionTimedOut,
        pendingDecision: decision && !decision.confirmedAt
          ? { missingSku: decision.missingSku, missingName: decision.missingName,
              requiredQty: decision.requiredQty, available: decision.available,
              alternatives: decision.alternatives, chosen: decision.chosen }
          : null,
        substitutions: bom.filter(item => item.substitutedFor)
          .map(item => ({ from: item.substitutedFor, to: item.sku, qty: item.requiredQty })),
        done: doneCount(),
        total: bom.length,
        stockLevels: { ...levels },
      }
    },

    bom: bom,
    events,
  }
}
