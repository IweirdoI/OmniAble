// SSE framing and chat state are independent of Vue so failure paths are testable.
export class SSEParser {
  constructor() { this.decoder = new TextDecoder(); this.buffer = ''; this.data = [] }
  feed(bytes, final = false) {
    this.buffer += bytes ? this.decoder.decode(bytes, { stream: !final }) : this.decoder.decode()
    const events = []
    const dispatch = () => {
      if (!this.data.length) return
      const text = this.data.join('\n'); this.data = []
      let event
      try { event = JSON.parse(text) } catch { throw new Error('服务端返回了无效的流式数据') }
      if (!event || typeof event.type !== 'string') throw new Error('流式事件缺少类型')
      events.push(event)
    }
    while (true) {
      const match = /[\r\n]/.exec(this.buffer)
      if (!match) break
      const i = match.index
      if (this.buffer[i] === '\r' && i === this.buffer.length - 1 && !final) break
      const length = this.buffer.slice(i, i + 2) === '\r\n' ? 2 : 1
      const line = this.buffer.slice(0, i); this.buffer = this.buffer.slice(i + length)
      if (!line) dispatch()
      else if (line.startsWith('data:')) this.data.push(line.slice(5).replace(/^ /, ''))
    }
    if (final && (this.buffer || this.data.length)) throw new Error('流式事件未完整结束')
    if (this.buffer.length > 1_000_000 || this.data.join('').length > 1_000_000) throw new Error('流式事件超过大小上限')
    return events
  }
}

export async function streamChat(payload, onEvent, { headers = {}, signal, timeout = 240000, fetcher = fetch } = {}) {
  const controller = new AbortController()
  let timedOut = false, reader
  const abort = () => controller.abort()
  if (signal?.aborted) abort()
  signal?.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(() => { timedOut = true; abort() }, timeout)
  let terminal = false, runId = null, seq = 0
  const receive = events => {
    // Validate the complete batch before callbacks can commit successful history.
    for (const event of events) {
      if (terminal) throw new Error('收到重复或终态之后的流式事件')
      if (event.run_id) {
        if (runId && runId !== event.run_id) throw new Error('流式请求标识不一致')
        runId = event.run_id
        if (!Number.isInteger(event.seq) || event.seq <= seq) throw new Error('流式事件顺序无效')
        seq = event.seq
      }
      terminal = event.type === 'done' || event.type === 'error'
    }
    for (const event of events) onEvent(event)
  }
  try {
    const requestHeaders = new Headers(headers)
    requestHeaders.set('Content-Type', 'application/json')
    const response = await fetcher('/api/agent/chat/stream', {
      method: 'POST', headers: requestHeaders,
      body: JSON.stringify(payload), signal: controller.signal,
    })
    if (!response.ok) {
      let detail
      try { detail = (await response.json()).detail } catch {}
      throw new Error((typeof detail === 'string' ? detail : detail?.message) || `请求失败（${response.status}）`)
    }
    reader = response.body.getReader()
    const parser = new SSEParser()
    while (!terminal) {
      const { done, value } = await reader.read()
      if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError')
      receive(parser.feed(value, done))
      if (done) break
    }
    if (!terminal) throw new Error('连接提前结束，未收到完成结果；已输出内容仅为部分结果')
  } catch (error) {
    if (controller.signal.aborted) {
      const failure = new Error(timedOut ? '请求超时，后端可能仍在处理，请同步确认结果' : '已请求停止；已经开始的操作可能完成，请同步确认结果')
      failure.name = timedOut ? 'TimeoutError' : 'AbortError'
      throw failure
    }
    throw error
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
    if (reader) { try { await reader.cancel() } catch {} reader.releaseLock() }
  }
}

export function applyChatEvent(message, event) {
  if (event.type === 'run_started') message.runId = event.run_id
  else if (event.type === 'token') message.content += event.text || ''
  else if (event.type === 'stage') {
    message.stage = event.stage
    message.phases ||= []
    message.phases = [...message.phases.filter(p => p.name !== event.stage), { name: event.stage, phase: event.phase }]
  } else if (event.type === 'tool_start') {
    message.steps.push({ id: event.id, name: event.name, args: event.args || {}, status: 'running', result: '' })
  } else if (event.type === 'tool_result') {
    const step = message.steps.find(s => event.id ? s.id === event.id : s.name === event.name && s.status === 'running')
    if (step) { step.status = event.ok ? 'done' : 'failed'; step.result = event.content || '' }
  } else if (event.type === 'done' || event.type === 'error') {
    message.status = event.workflow?.status || (event.type === 'error' ? 'failed' : 'completed')
    message.workflow = event.workflow
    message.memoryWrite = event.memory_write?.status
    if (event.type === 'done') message.content = event.answer ?? message.content
    else message.failure = event.message || '智能体执行失败'
    for (const step of message.steps) if (step.status === 'running') step.status = 'interrupted'
    for (const phase of message.phases || []) if (phase.phase !== 'end') phase.phase = 'interrupted'
  }
}
