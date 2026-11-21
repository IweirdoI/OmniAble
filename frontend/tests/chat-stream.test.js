import test from 'node:test'
import assert from 'node:assert/strict'
import { SSEParser, streamChat, applyChatEvent } from '../src/chat-stream.js'

const enc = new TextEncoder()
const frame = event => `data: ${JSON.stringify(event)}\n\n`
const response = text => async () => new Response(text)
const message = () => ({ content: '', steps: [], status: 'running' })

test('UTF-8 byte boundaries, split CRLF, comments and multiple data lines', () => {
  const bytes = enc.encode(': heartbeat\r\ndata: {"type":\r\ndata: "token", "text":"中文"}\r\n\r\n')
  const parser = new SSEParser(), events = []
  for (const byte of bytes) events.push(...parser.feed(Uint8Array.of(byte)))
  events.push(...parser.feed(null, true))
  assert.deepEqual(events, [{ type: 'token', text: '中文' }])
})

test('truncated JSON and malformed application events fail visibly', async () => {
  for (const input of ['data: bad\n\n', 'data: {"answer":1}\n\n', 'data: {"type":"token"}']) {
    await assert.rejects(streamChat({}, () => {}, { fetcher: response(input) }))
  }
})

test('empty EOF and token-only EOF cannot be successful', async () => {
  for (const input of ['', frame({ type: 'token', text: 'partial' })]) {
    await assert.rejects(streamChat({}, () => {}, { fetcher: response(input) }), /未收到完成结果/)
  }
})

test('done.answer replaces tool-round and uncleaned tokens; legacy server remains compatible', async () => {
  const msg = message()
  await streamChat({}, event => applyChatEvent(msg, event), { fetcher: response(
    frame({ type: 'token', text: '<think>intermediate</think>' }) + frame({ type: 'done', answer: '最终答案' })) })
  assert.equal(msg.content, '最终答案')
  assert.equal(msg.status, 'completed')
})

test('partial output followed by error keeps text and exposes failure', async () => {
  const msg = message()
  await streamChat({}, event => applyChatEvent(msg, event), { fetcher: response(
    frame({ type: 'token', text: '未完成内容' }) + frame({ type: 'error', message: '模型失败', workflow: { status: 'failed' } })) })
  assert.equal(msg.content, '未完成内容')
  assert.equal(msg.failure, '模型失败')
  assert.equal(msg.status, 'failed')
})

test('same-name tools match their actual call IDs', () => {
  const msg = message()
  for (const id of ['a', 'b']) applyChatEvent(msg, { type: 'tool_start', id, name: 'read_file' })
  applyChatEvent(msg, { type: 'tool_result', id: 'b', ok: false, content: 'missing' })
  assert.equal(msg.steps[0].status, 'running')
  assert.equal(msg.steps[1].status, 'failed')
  applyChatEvent(msg, { type: 'error', message: 'stop' })
  assert.equal(msg.steps[0].status, 'interrupted')
})

test('mixed request IDs and non-monotonic sequence numbers are rejected', async () => {
  for (const next of [{ run_id: 'other', seq: 2 }, { run_id: 'first', seq: 1 }]) {
    await assert.rejects(streamChat({}, () => {}, { fetcher: response(
      frame({ type: 'run_started', run_id: 'first', seq: 1 }) + frame({ type: 'done', ...next })) }))
  }
})

test('stop and timeout abort transport and cancel its reader', async () => {
  for (const timedOut of [false, true]) {
    let transportSignal
    const controller = new AbortController()
    const fetcher = async (_, options) => { transportSignal = options.signal; return new Response(new ReadableStream({
      start(stream) { options.signal.addEventListener('abort', () => stream.error(new DOMException('Aborted', 'AbortError'))) },
    })) }
    const run = streamChat({}, () => {}, { fetcher, signal: controller.signal, timeout: timedOut ? 10 : 1000 })
    if (!timedOut) setTimeout(() => controller.abort(), 5)
    await assert.rejects(run, error => error.name === (timedOut ? 'TimeoutError' : 'AbortError') && /可能完成|可能仍在/.test(error.message))
    assert.equal(transportSignal.aborted, true)
  }
})

test('reader is cancelled after terminal even when upstream does not close', async () => {
  let cancelled = false
  const fetcher = async () => new Response(new ReadableStream({
    start(stream) { stream.enqueue(enc.encode(frame({ type: 'done', answer: 'ok' }))) },
    cancel() { cancelled = true },
  }))
  await streamChat({}, () => {}, { fetcher })
  assert.equal(cancelled, true)
})

test('HTTP overload and structured timeout details remain actionable', async () => {
  for (const [status, detail] of [[429, '并发已满'], [504, { message: '超过预算' }]]) {
    await assert.rejects(streamChat({}, () => {}, { fetcher: async () => Response.json({ detail }, { status }) }), /并发已满|超过预算/)
  }
})

test('Headers instances preserve authentication on the SSE request', async () => {
  await streamChat({}, () => {}, { headers: new Headers({ Authorization: 'Bearer synthetic' }), fetcher: async (_, options) => {
    assert.equal(options.headers.get('Authorization'), 'Bearer synthetic')
    assert.equal(options.headers.get('Content-Type'), 'application/json')
    return new Response(frame({ type: 'done', answer: 'ok' }))
  } })
})

test('invalid terminal batch is rejected before successful-history callbacks', async () => {
  const events = []
  await assert.rejects(streamChat({}, e => events.push(e), { fetcher: response(
    frame({ type: 'done', answer: 'unconfirmed' }) + frame({ type: 'error', message: 'duplicate' })) }))
  assert.deepEqual(events, [])
})
