// Browser regression for TASK-018 local session list & recovery (Issue #23).
// Runs against Vite plus the isolated API fixture; chat streams are mocked for
// deterministic timing while the session endpoints use the real backend store.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const assert = require('node:assert/strict')
const { captureFailure, launchOptions } = require('./browser-artifacts.cjs')

const base = new URL(process.env.FRONTEND_TEST_URL || 'http://127.0.0.1:5174')
base.searchParams.set('view', 'chat')
const check = (name) => console.log('PASS', name)

const frame = (event) => `data: ${JSON.stringify(event)}\n\n`
const doneFrame = (answer) => frame({
  type: 'done', answer, memories: [], recall_ms: 1, latency_ms: 2, warnings: [],
  workflow: { status: 'completed' },
})

const waitConnected = (page) => page.waitForFunction(
  () => document.querySelector('[data-testid=connection-button]')?.textContent.includes('已连接'))
const send = async (page, message) => {
  await page.getByTestId('chat-input').fill(message)
  await page.getByTestId('chat-send').click()
}
const waitDone = (page) => page.waitForFunction(() =>
  [...document.querySelectorAll('[data-testid=run-status]')]
    .some(el => /已完成/.test(el.textContent || '')))

;(async () => {
  const browser = await chromium.launch(launchOptions())
  let page = null
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    page.setDefaultTimeout(15000)
    await page.goto(base.href, { waitUntil: 'networkidle' })
    await waitConnected(page)

    // 1) 首轮发送：请求带 session_id 且不再自证 history；完成后列表出现会话
    await page.route('**/api/agent/chat/stream', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: doneFrame('第一条回答') })
    })
    const firstRequest = page.waitForRequest(req => req.url().endsWith('/api/agent/chat/stream'))
    await send(page, '第一条消息')
    const payload = await (await firstRequest).postDataJSON()
    assert.ok(payload.session_id, '首轮发送应携带服务端会话 id')
    assert.equal(payload.history, undefined, '带会话时上下文由服务端构建，不再发送前端 history')
    await waitDone(page)
    await page.waitForFunction(() => document.querySelector('[data-testid^=session-open-]'))
    assert.match(await page.locator('.session-rail').innerText(), /第一条消息/)
    await page.unroute('**/api/agent/chat/stream')
    check('first turn persists into the session list with server-built history')

    // 2) 重启恢复：刷新后列表仍在，点击恢复已完成对话（历史消息不带运行状态）
    await page.reload({ waitUntil: 'networkidle' })
    await waitConnected(page)
    await page.locator('[data-testid^=session-open-]').first().click()
    await page.waitForFunction(() =>
      document.querySelector('.chat-stream')?.textContent.includes('第一条消息'))
    assert.match(await page.locator('.chat-stream').innerText(), /第一条回答/)
    assert.equal(await page.getByTestId('run-status').count(), 0,
      '恢复的历史消息不得带运行状态')
    check('restart recovery restores the completed conversation')

    // 3) 用户隔离：user_b 看不到 demo_user 的会话，切回后各自的列表互不串扰
    await page.route('**/api/agent/chat/stream', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: doneFrame('B 的回答') })
    })
    await page.getByTestId('user-input').fill('user_b')
    await page.getByTestId('user-switch').click()
    await page.waitForFunction(() =>
      document.querySelector('[data-testid=active-user]')?.textContent.includes('user_b'))
    await page.waitForFunction(() =>
      document.querySelector('.session-rail')?.textContent.includes('还没有已保存的会话'))
    const bRequest = page.waitForRequest(req => req.url().endsWith('/api/agent/chat/stream'))
    await send(page, 'B 的消息')
    const bPayload = await (await bRequest).postDataJSON()
    assert.notEqual(bPayload.session_id, payload.session_id, '不同用户的会话必须隔离')
    await waitDone(page)
    await page.getByTestId('user-input').fill('demo_user')
    await page.getByTestId('user-switch').click()
    await page.waitForFunction(() =>
      document.querySelector('[data-testid=active-user]')?.textContent.includes('demo_user'))
    await page.waitForFunction(() =>
      document.querySelector('.session-rail')?.textContent.includes('第一条消息'))
    assert.ok(!(await page.locator('.chat-stream').innerText()).includes('B 的回答'),
      '切回后不得显示其他用户的对话内容')
    await page.unroute('**/api/agent/chat/stream')
    check('sessions and in-chat content stay isolated across users')

    // 4) 删除会话：确认后列表移除、当前对话回到未保存态
    page.on('dialog', (dialog) => dialog.accept())
    await page.locator('.session-delete').first().click()
    await page.waitForFunction(() =>
      document.querySelector('.session-rail')?.textContent.includes('还没有已保存的会话'))
    assert.equal(await page.locator('[data-testid^=session-open-]').count(), 0)
    check('deleting a session removes it from the rail and resets the chat')

    // 5) 中断不冒充完成：EOF 回合不落库，重启后聊天区不出现该轮内容
    await page.route('**/api/agent/chat/stream', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    })
    await send(page, 'EOF 消息')
    await page.getByTestId('run-failure').waitFor()
    assert.match(await page.getByTestId('run-failure').innerText(), /连接提前结束/)
    await page.reload({ waitUntil: 'networkidle' })
    await waitConnected(page)
    const restoredText = await page.locator('.chat-stream').innerText()
    assert.ok(!restoredText.includes('EOF 消息'), '中断回合不得在重启后冒充完成')
    check('interrupted turn is never restored as a completed conversation')

    console.log('All session browser checks passed.')
  } catch (error) {
    await captureFailure(page, 'session', error)
    throw error
  } finally {
    await browser.close()
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
