// TASK-027：桌面命令目录。稳定命令名 + 中文别名 + 帮助 + 可访问名称 +
// 状态播报文本，独立于 Vue 可单测。命令只登记既有动作处理器，不新增
// 并行入口（对齐 DESKTOP_AGENT_ROADMAP 的"同一份结果驱动显示与播报"）。

const normalize = (text) => String(text || '').toLowerCase().replace(/\s+/g, '')

export function createCommandCatalog() {
  const items = []
  const seen = new Set()

  function register(command) {
    if (Array.isArray(command)) {
      for (const item of command) register(item)
      return command
    }
    const id = command.id
    if (!id || !command.name || typeof command.run !== 'function') {
      throw new Error('命令缺少 id、名称或处理器')
    }
    if (seen.has(id)) throw new Error(`重复命令 id：${id}`)
    seen.add(id)
    items.push({
      aliases: [],
      keywords: [],
      help: '',
      enabled: null,
      ...command,
    })
    return command
  }

  function all() {
    return items.filter((item) => !item.enabled || item.enabled())
  }

  // 匹配口径：别名/名称完全相等 > 名称或别名前缀 > 关键词/帮助子串。
  // 空查询返回全部可用命令（面板初始列表）。
  function match(query) {
    const q = normalize(query)
    const available = all()
    if (!q) return available
    const scored = []
    for (const item of available) {
      const names = [item.name, ...item.aliases].map(normalize)
      let score = 0
      if (names.includes(q)) score = 100
      else if (names.some((name) => name.startsWith(q))) score = 70
      else if (names.some((name) => name.includes(q))) score = 50
      else if ([item.id, ...item.keywords, item.help].some(
          (text) => normalize(text).includes(q))) score = 30
      if (score) scored.push({ item, score })
    }
    scored.sort((a, b) => b.score - a.score
      || a.item.name.localeCompare(b.item.name, 'zh-Hans-CN'))
    return scored.map((entry) => entry.item)
  }

  function get(id) {
    return items.find((item) => item.id === id) || null
  }

  return { register, all, match, get }
}

// ---------------------------------------------------------------------------
// TASK-027：读屏播报受控。同一文本在窗口期内只播报一次，不同文本立即播报；
// toast 等视觉反馈不受影响——只有 aria-live 文本走这里。
// ---------------------------------------------------------------------------
export function createAnnouncer({ dedupeMs = 500, now = Date.now } = {}) {
  let lastText = ''
  let lastAt = -Infinity
  let dropped = 0

  function announce(text) {
    const value = String(text || '')
    if (!value) return { announced: false, reason: 'empty' }
    const at = now()
    if (value === lastText && at - lastAt < dedupeMs) {
      dropped += 1
      return { announced: false, reason: 'deduped', dropped }
    }
    lastText = value
    lastAt = at
    dropped = 0
    return { announced: true, dropped: 0 }
  }

  return { announce }
}
