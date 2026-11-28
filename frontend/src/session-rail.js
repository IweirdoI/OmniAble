// 会话侧栏状态与操作独立于 Vue，失败路径可单测（TASK-018 / Issue #23 UI 收尾）。
// 语义对齐 api/server.py 会话端点：列表/新建/详情/删除均按 user_id 隔离；
// 过期响应一律丢弃（操作号守卫），切换用户/会话后旧请求不回填。

const timeText = (iso) => {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '' :
    new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(date)
}

// 服务端只存已完成回合；suppressed 消息正文已是占位文案（见 session_store._display_message）。
const mapStoredMessage = (m) => ({
  id: 'session-' + m.id,
  role: m.role === 'user' ? 'user' : 'assistant',
  content: m.content,
  time: timeText(m.created_at),
  restored: true,
  suppressed: !!m.suppressed,
})

export function createSessionRail(state, request, now = () => '') {
  let listVersion = 0
  let openVersion = 0

  function reset() {
    listVersion += 1
    openVersion += 1
    state.status = 'idle'
    state.items = []
    state.total = 0
    state.error = null
    state.updatedAt = null
  }

  // 列表：offset=0 重置分页；offset>0 追加（加载更多）。过期响应丢弃。
  async function load(user, { offset = 0, limit = 50 } = {}) {
    const version = ++listVersion
    if (offset === 0) state.status = 'loading'
    try {
      const data = await request(
        `/agent/sessions/${encodeURIComponent(user)}?offset=${offset}&limit=${limit}`)
      if (version !== listVersion) return false
      state.items = offset === 0 ? data.items : [...state.items, ...data.items]
      state.total = data.total
      state.status = 'ready'
      state.error = null
      state.updatedAt = now()
      return true
    } catch (error) {
      if (version !== listVersion) return false
      state.error = error.message
      // 首屏失败且无旧数据 → error（不拿空列表冒充"没有会话"）；有旧数据 → stale 保留
      if (offset === 0) state.status = state.items.length ? 'stale' : 'error'
      return false
    }
  }

  async function create(user, title) {
    const data = await request('/agent/sessions', {
      method: 'POST',
      body: JSON.stringify({ user_id: user, title }),
    })
    return data.session
  }

  // 详情：返回会话与映射后的消息；请求失败正常抛出、被更新的打开取代时返回 null
  // （调用方对 null 与抛错都不得回填聊天区）。
  async function open(user, sessionId) {
    const version = ++openVersion
    const data = await request(
      `/agent/sessions/${encodeURIComponent(user)}/${encodeURIComponent(sessionId)}`)
    if (version !== openVersion) return null
    const session = data.session
    return { session, messages: (session.messages || []).map(mapStoredMessage) }
  }

  async function remove(user, sessionId) {
    const data = await request(
      `/agent/sessions/${encodeURIComponent(user)}/${encodeURIComponent(sessionId)}`,
      { method: 'DELETE' })
    return !!data.deleted
  }

  return { reset, load, create, open, remove }
}
