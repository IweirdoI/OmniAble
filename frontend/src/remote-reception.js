// 远程接待与现场设备协作原型（TASK-047 / Issue #67）
// 纯逻辑状态机：虚构商品目录 + 观察流 + 规格查询 + 询价记录 + 控制权互斥与设备动作确认 + 断联冻结。
// 模拟设备不接真机；设计、交互稿与复用点见 docs/REMOTE_RECEPTION.md。
// 语义对齐：动作经唯一入口分发与执行前拒绝（src/agent/actions.py dispatch_action）、
// 控制类动作绑定确认与期望状态版本（src/agent/confirmations.py）、
// 重复请求幂等且中断/作废不自动重发（src/agent/run_store.py）。
// 两个正交状态：linkFrozen 只管远程链路（现场不受限）；controlState 只反映控制权持有者。

export const LINK_TIMEOUT_MS = 45000

export const ROLES = {
  REMOTE_STAFF: 'remote_staff',      // 远程工作人员：业务判断，可观察、可经确认控制
  SITE_ASSISTANT: 'site_assistant',  // 现场协助者：本地处置，可随时接管控制权
}

export const CONTROL_STATE = {
  IDLE: 'idle',              // 无控制权持有者
  REMOTE: 'remote_control',  // 远程工作人员控制中
  SITE: 'site_control',      // 现场协助者控制中
}

export const DEVICE_ACTIONS = ['switch_display', 'move_sample']

export function createShowcaseCatalog() {
  // 虚构商品与规格，全部合成数据；specVersion 对应"来源版本"语义，规格更新后引用需复核。
  return [
    {
      key: 'cup-yunwen', name: '云纹陶瓷杯', slot: 1, specVersion: 2,
      specs: { 材质: '陶瓷', 容量: '350 毫升', 颜色: '青白' },
      description: '展示转台 1 号位：青白釉陶瓷杯，杯身一圈云纹浮雕，杯口无缺口。',
    },
    {
      key: 'box-walnut', name: '胡桃木收纳盒', slot: 2, specVersion: 1,
      specs: { 材质: '胡桃木', 尺寸: '20×14×9 厘米', 隔层: '3 层' },
      description: '展示转台 2 号位：胡桃木三层收纳盒，盒盖闭合。',
    },
    {
      key: 'frame-acrylic', name: '亚克力相框', slot: 3, specVersion: 1,
      specs: { 材质: '亚克力', 尺寸: '六寸', 摆放: '立式' },
      description: '展示转台 3 号位：立式亚克力相框，表面无划痕。',
    },
  ]
}

const fail = (error, message) => ({ ok: false, error, message })

// 角色到控制状态的映射：现场协助者持有即 site_control，否则 remote_control。
const stateForRole = actor =>
  actor?.role === ROLES.SITE_ASSISTANT ? CONTROL_STATE.SITE : CONTROL_STATE.REMOTE

export function createRemoteSession({
  catalog = createShowcaseCatalog(),
  now = () => Date.now(),
  linkTimeoutMs = LINK_TIMEOUT_MS,
} = {}) {
  const byKey = new Map(catalog.map(item => [item.key, { ...item, specs: { ...item.specs } }]))
  const device = { displaySlot: 1, stateVersion: 1 }
  let controlState = CONTROL_STATE.IDLE
  let holder = null
  let linkFrozen = false
  const observeGrants = new Set()
  const drafts = new Map()
  const inquiries = []
  const confirmations = new Map()
  const events = []
  let link = { active: false, lastBeat: 0, voided: [] }

  const log = (kind, detail) => events.push({ kind, detail, at: now() })
  const isRemote = actor => actor?.role === ROLES.REMOTE_STAFF
  const isSite = actor => actor?.role === ROLES.SITE_ASSISTANT

  // 远程心跳超时即冻结链路（显式断联走 remoteDisconnect）；现场侧不受影响。
  function tick() {
    if (!linkFrozen && link.active && now() - link.lastBeat > linkTimeoutMs) {
      freeze('heartbeat_timeout')
    }
  }

  function freeze(reason) {
    linkFrozen = true
    link.voided = []
    for (const confirm of confirmations.values()) {
      if (confirm.status === 'pending' && isRemote(confirm.actor)) {
        confirm.status = 'voided'
        confirm.note = '远程连接中断，确认已作废；实际结果需现场核对，不会自动重发。'
        link.voided.push(confirm)
      }
    }
    if (holder && isRemote(holder)) {
      holder = null
      controlState = CONTROL_STATE.IDLE
    }
    log('link_frozen', reason)
  }

  function refreshBeat(actor) {
    if (isRemote(actor)) link = { ...link, active: true, lastBeat: now() }
  }

  const grant = actor => {
    tick()
    if (linkFrozen && isRemote(actor)) {
      return fail('link_frozen', '远程连接已中断，请等现场处置或重新连接。')
    }
    refreshBeat(actor)
    return null
  }

  return {
    tick,

    grantObserve(actor) {
      if (!actor?.name || (!isRemote(actor) && !isSite(actor))) {
        return fail('unknown_actor', '观察授权需要带名字的远程工作人员或现场协助者。')
      }
      observeGrants.add(actor.name)
      log('observe_granted', actor.name)
      return { ok: true, message: `已授予 ${actor.name} 观察权限；观察是只读的，不驱动设备。` }
    },

    revokeObserve(actor, target) {
      if (!isSite(actor)) {
        return fail('role_forbidden', '观察权限由现场协助者管理。')
      }
      if (!observeGrants.delete(target)) {
        return fail('not_granted', `${target} 当前没有观察权限。`)
      }
      log('observe_revoked', target)
      return { ok: true, message: `已撤销 ${target} 的观察权限。` }
    },

    observeProduct(actor, key) {
      const blocked = grant(actor)
      if (blocked) return blocked
      if (!observeGrants.has(actor.name)) {
        return fail('observe_required', '请先取得观察权限再查看现场。')
      }
      const product = byKey.get(key)
      if (!product) return fail('unknown_target', `展示架上没有商品 ${key}。`)
      const onDisplay = device.displaySlot === product.slot
      return {
        ok: true,
        snapshot: {
          key, name: product.name, slot: product.slot, onDisplay,
          description: onDisplay ? product.description : `${product.name} 在 ${product.slot} 号位，当前未在转台展示位。`,
          deviceStateVersion: device.stateVersion,
        },
      }
    },

    querySpec(actor, key, { expectedSpecVersion } = {}) {
      const blocked = grant(actor)
      if (blocked) return blocked
      const product = byKey.get(key)
      if (!product) return fail('unknown_target', `展示架上没有商品 ${key}。`)
      const stale = expectedSpecVersion != null &&
        Number(expectedSpecVersion) !== product.specVersion
      return {
        ok: true,
        key, name: product.name, specs: { ...product.specs },
        specVersion: product.specVersion,
        stale,
        message: stale
          ? `规格已更新到 v${product.specVersion}，引用旧版本前请复核。`
          : `规格 v${product.specVersion} 为当前版本。`,
      }
    },

    updateSpec(actor, key, specs) {
      const blocked = grant(actor)
      if (blocked) return blocked
      const product = byKey.get(key)
      if (!product) return fail('unknown_target', `展示架上没有商品 ${key}。`)
      const mayEdit = isSite(actor) || holder?.name === actor.name
      if (!mayEdit) return fail('edit_forbidden', '更新规格需要控制权或现场协助者身份。')
      product.specs = { ...specs }
      product.specVersion += 1
      log('spec_updated', `${key} -> v${product.specVersion}`)
      return { ok: true, specVersion: product.specVersion, message: `${product.name} 规格已更新到 v${product.specVersion}。` }
    },

    draftInquiry(actor, { productKey, customerAsk, answer }) {
      const blocked = grant(actor)
      if (blocked) return blocked
      const product = byKey.get(productKey)
      if (!product) return fail('unknown_target', `展示架上没有商品 ${productKey}。`)
      const ask = String(customerAsk || '').trim()
      if (!ask) return fail('invalid_arguments', '请填写客户咨询内容。')
      const draftId = `draft-${drafts.size + 1}`
      const draft = {
        draftId, productKey, productName: product.name, customerAsk: ask,
        answer: String(answer || '').trim(), specVersion: product.specVersion,
        createdAt: now(),
      }
      drafts.set(draftId, draft)
      return { ok: true, draft, message: '询价记录已保存为草稿；本人确认后才写入记录。' }
    },

    confirmInquiry(actor, draftId) {
      const blocked = grant(actor)
      if (blocked) return blocked
      const draft = drafts.get(draftId)
      if (!draft) return fail('unknown_draft', `没有草稿 ${draftId}。`)
      const existing = inquiries.find(item => item.draftId === draftId)
      if (existing) {
        return { ok: true, duplicate: true, record: existing, message: '此草稿已确认过；重复确认不会产生第二条记录。' }
      }
      const product = byKey.get(draft.productKey)
      if (product.specVersion !== draft.specVersion) {
        return fail('source_updated', `规格已从 v${draft.specVersion} 更新到 v${product.specVersion}，请重新起草并复核后再确认。`)
      }
      const record = { ...draft, inquiryId: `inq-${inquiries.length + 1}`, confirmedAt: now() }
      inquiries.push(record)
      log('inquiry_confirmed', record.inquiryId)
      return { ok: true, record, message: `询价记录 ${record.inquiryId} 已确认；重复确认不会重复执行。` }
    },

    listInquiries() {
      return { ok: true, inquiries: [...inquiries] }
    },

    remoteConnect(actor) {
      if (!isRemote(actor)) return fail('role_forbidden', '只有远程工作人员需要建立远程连接。')
      linkFrozen = false
      link = { active: true, lastBeat: now(), voided: [] }
      log('remote_connected', actor.name)
      return { ok: true, message: '远程连接已建立；控制权需另行请求。' }
    },

    remoteHeartbeat(actor) {
      if (!isRemote(actor)) return fail('role_forbidden', '只有远程工作人员发送远程心跳。')
      refreshBeat(actor)
      return { ok: true }
    },

    remoteDisconnect(actor, reason = 'remote_quit') {
      if (!isRemote(actor)) return fail('role_forbidden', '只有远程工作人员可以断开远程连接。')
      freeze(reason)
      return { ok: true, message: '远程连接已断开；现场协助者仍可操作，作废请求不会自动重发。' }
    },

    reconnectRemote(actor) {
      if (!isRemote(actor)) return fail('role_forbidden', '只有远程工作人员可以重新连接。')
      if (!linkFrozen) return fail('not_frozen', '远程连接并未中断。')
      const voided = [...link.voided]
      linkFrozen = false
      link = { active: true, lastBeat: now(), voided: [] }
      log('remote_reconnected', actor.name)
      return {
        ok: true,
        voided: voided.map(item => ({ confirmId: item.confirmId, action: item.action, targetKey: item.targetKey })),
        controlState,
        message: holder
          ? `已重新连接；控制权仍由 ${holder.name} 持有，需其释放后重新请求。作废的确认不会自动重发。`
          : '已重新连接；控制权不自动恢复，需要重新请求。作废的确认不会自动重发。',
      }
    },

    requestControl(actor) {
      const blocked = grant(actor)
      if (blocked) return blocked
      if (holder?.name === actor.name) {
        return fail('already_holding', '控制权已在你手中。')
      }
      if (holder && isRemote(holder) && isRemote(actor)) {
        return fail('control_busy', `控制权由 ${holder.name} 持有；请勿抢控制权，可请现场协助者接管。`)
      }
      if (holder) {
        return fail('control_busy', `控制权由 ${holder.name}（${isSite(holder) ? '现场' : '远程'}）持有；现场协助者可接管。`)
      }
      holder = { name: actor.name, role: actor.role }
      controlState = stateForRole(actor)
      log('control_granted', actor.name)
      return { ok: true, controlState, message: isSite(actor) ? '现场协助者已取得控制权。' : '控制权已授予远程工作人员；设备动作仍需逐次确认。' }
    },

    takeoverBySite(actor) {
      const blocked = grant(actor)
      if (blocked) return blocked
      if (!isSite(actor)) return fail('role_forbidden', '只有现场协助者可以现场接管。')
      const previous = holder
      const voided = []
      for (const confirm of confirmations.values()) {
        if (confirm.status === 'pending' && previous && confirm.actor.name === previous.name) {
          confirm.status = 'voided'
          confirm.note = '现场已接管，待确认的控制请求已作废；实际结果需现场核对，不会自动重发。'
          voided.push(confirm)
        }
      }
      holder = { name: actor.name, role: actor.role }
      controlState = CONTROL_STATE.SITE
      log('site_takeover', previous ? `${previous.name} -> ${actor.name}` : actor.name)
      return {
        ok: true,
        voided: voided.map(item => ({ confirmId: item.confirmId, action: item.action })),
        message: '现场协助者已接管控制权；原持有者的待确认请求已作废，不会自动重发。',
      }
    },

    releaseControl(actor) {
      const blocked = grant(actor)
      if (blocked) return blocked
      if (holder?.name !== actor.name) return fail('not_holding', '你当前不持有控制权。')
      holder = null
      controlState = CONTROL_STATE.IDLE
      log('control_released', actor.name)
      return { ok: true, message: '控制权已释放。' }
    },

    requestDeviceAction(actor, { action, targetKey, expectedStateVersion }) {
      const blocked = grant(actor)
      if (blocked) return blocked
      if (!DEVICE_ACTIONS.includes(action)) {
        return fail('unknown_action', `未知设备动作 ${action}；可用：${DEVICE_ACTIONS.join('、')}。`)
      }
      if (holder?.name !== actor.name) {
        return fail('control_required', '设备动作需要先取得控制权。')
      }
      const target = byKey.get(targetKey)
      if (!target) return fail('unknown_target', `展示架上没有商品 ${targetKey}。`)
      if (!Number.isInteger(expectedStateVersion)) {
        return fail('invalid_arguments', '设备动作必须携带期望的设备状态版本。')
      }
      const confirmId = `confirm-${confirmations.size + 1}`
      const record = {
        confirmId, token: `token-${confirmId}-${now()}`, runId: `run-${confirmations.size + 1}`,
        action, targetKey, expectedStateVersion, actor,
        status: 'pending', decidedAt: null, note: null,
      }
      confirmations.set(confirmId, record)
      log('device_confirm_requested', `${action} -> ${targetKey}`)
      return {
        ok: true,
        confirmation: {
          confirmId, runId: record.runId, action, targetKey,
          targetName: target.name, expectedStateVersion, currentStateVersion: device.stateVersion,
          summary: action === 'switch_display'
            ? `把展示转台切到 ${target.name}（${target.slot} 号位）。`
            : `由机械臂把 ${target.name} 移到展示位（模拟预设轻小物品动作）。`,
        },
        decisionToken: record.token,
      }
    },

    decideDeviceAction(actor, confirmId, { decision, token } = {}) {
      const blocked = grant(actor)
      if (blocked) return blocked
      const record = confirmations.get(confirmId)
      if (!record) return fail('unknown_confirmation', `没有确认请求 ${confirmId}。`)
      if (token !== record.token) return fail('bad_token', '确认令牌不匹配；决定未被接受。')
      if (record.status !== 'pending') {
        return {
          ok: true, duplicate: true, status: record.status,
          message: `此确认已有结果（${record.status}）；重复提交返回同一结果。`,
        }
      }
      record.decidedAt = now()
      if (decision !== 'approve') {
        record.status = 'denied'
        return { ok: true, status: 'denied', message: '已拒绝该设备动作；设备未变化。' }
      }
      if (device.stateVersion !== record.expectedStateVersion) {
        record.status = 'revision_changed'
        record.note = '设备状态在确认期间发生变化；请重新读取快照后再次发起。'
        return fail('revision_changed', record.note)
      }
      const target = byKey.get(record.targetKey)
      if (record.action === 'switch_display') {
        device.displaySlot = target.slot
      }
      device.stateVersion += 1
      record.status = 'executed'
      log('device_executed', `${record.action} -> ${record.targetKey} @v${device.stateVersion}`)
      return {
        ok: true, status: 'executed', deviceStateVersion: device.stateVersion,
        message: record.action === 'switch_display'
          ? `展示转台已切到 ${target.name}（${target.slot} 号位）。`
          : `机械臂已把 ${target.name} 移到展示位（模拟完成）。`,
      }
    },

    snapshot() {
      return {
        controlState,
        linkFrozen,
        holder: holder ? { ...holder } : null,
        device: { ...device },
        displayProduct: [...byKey.values()].find(item => item.slot === device.displaySlot)?.name || null,
        pendingConfirmations: [...confirmations.values()]
          .filter(item => item.status === 'pending')
          .map(item => ({ confirmId: item.confirmId, action: item.action, targetKey: item.targetKey })),
        inquiryCount: inquiries.length,
      }
    },

    events,
  }
}
