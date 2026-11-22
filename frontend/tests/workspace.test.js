import test from 'node:test'
import assert from 'node:assert/strict'
import { normalizeWebUrl, fileLink, diffKind, workspaceRequest } from '../src/workspace.js'
import { applyChatEvent } from '../src/chat-stream.js'
import { setApiToken } from '../src/ingestion.js'

test('web URLs normalize and reject executable schemes, credentials and whitespace', () => {
  assert.equal(normalizeWebUrl('example.com/a?q=1'), 'https://example.com/a?q=1')
  assert.equal(normalizeWebUrl('http://localhost:5188'), 'http://localhost:5188/')
  for (const input of ['', 'javascript://evil', 'data://text/html', 'file:///C:/a.txt', 'https://u:p@example.com', 'https://example.com/ a']) {
    assert.throws(() => normalizeWebUrl(input))
  }
})

test('relative markdown file links cannot escape or become active schemes', () => {
  assert.equal(fileLink('./docs/guide.md#hello'), 'docs/guide.md')
  assert.equal(fileLink('notes/%E4%B8%AD.md'), 'notes/中.md')
  for (const input of ['../secret', '/etc/passwd', 'https://example.com', 'javascript:alert(1)', '//example.com', '#section']) assert.equal(fileLink(input), null)
})

test('diff styles treat headers separately from added/deleted source lines', () => {
  assert.equal(diffKind('+++ b/a'), 'header')
  assert.equal(diffKind('@@ -1 +1 @@'), 'hunk')
  assert.equal(diffKind('+x'), 'addition')
  assert.equal(diffKind('-x'), 'deletion')
})

test('tool arguments remain available for actual file and web sidebar links', () => {
  const msg = { steps: [] }
  applyChatEvent(msg, { type: 'tool_start', id: 'c1', name: 'read_file', args: { path: 'notes/a.txt' } })
  applyChatEvent(msg, { type: 'tool_result', id: 'c1', name: 'read_file', ok: true })
  assert.deepEqual(msg.steps[0].args, { path: 'notes/a.txt' })
  assert.equal(msg.steps[0].status, 'done')
})

test('workspace writes carry auth, original revision and content through shared transport', async () => {
  const oldFetch = globalThis.fetch
  setApiToken('synthetic-token')
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/workspace/file')
    assert.equal(options.headers.get('Authorization'), 'Bearer synthetic-token')
    assert.deepEqual(JSON.parse(options.body), { path: 'a.txt', content: 'draft', revision: null })
    return new Response(JSON.stringify({ changed: true }), { status: 200 })
  }
  try { assert.equal((await workspaceRequest('/file', { method: 'PUT', body: JSON.stringify({ path: 'a.txt', content: 'draft', revision: null }) })).changed, true) }
  finally { globalThis.fetch = oldFetch; setApiToken('') }
})
