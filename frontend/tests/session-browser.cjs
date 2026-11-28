// Browser regression for TASK-018 local session list & recovery (Issue #23).
// Runs against Vite plus the isolated API fixture. Turns 1-3 hit the REAL
// backend so completed turns are persisted; only the EOF scenario mocks the
// stream (the frontend honest-failure path). Dedicated user ids keep this
// suite independent of other suites' sessions in the shared fixture DB.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const assert = require('node:assert/strict')
const { captureFailure, launchOptions } = require('./browser-artifacts.cjs')

const base = new URL(process.env.FRONTEND_TEST_URL || 'http://127.0.0.1:5174')
base.searchParams.set('view', 'chat')
const check = (name) => console.log('PASS', name)

// 每次运行用独立用户 id：套件可重复执行，也不依赖 fixture 库的初始状态
const RUN = Date.now().toString(36)
const USER_A = `sess_e2e_a_${RUN}`
const USER_B = `sess_e2e_b_${RUN}`

const waitConnected = (page) => page.waitForFunction(
  () => document.querySelector('[data-testid=connection-button]')?.textContent.includes('已连接'))
const send = async (page, message) => {
  await page.getByTestId('chat-input').fill(message)
  await page.getByTestId('chat-send').click()
}
const waitDone = async (page) => {
  await page.waitForFunction(() =>
    [...document.querySelectorAll('[data-testid=run-status]')]
      .some(el => /已完成/.test(el.textContent || '')))
  // done 事件先于 sending 复位；等空闲再做切用户等被 sending 守卫的操作
  await page.waitForFunction(() =>
    !(document.querySelector('[data-testid=chat-send]')?.textContent.includes('处理中')))
}
const switchUser = async (page, next) => {
  await page.getByTestId('user-input').fill(next)
  await page.getByTestId('user-switch').click()
  await page.waitForFunction((user) =>
    document.querySelector('[data-testid=active-user]')?.textContent.includes(user), next)
}

;(async () => {
  const browser = await chromium.launch(launchOptions())
  let page = null
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    page.setDefaultTimeout(15000)
    await page.goto(base.href, { waitUntil: 'networkidle' })
    await waitConnected(page)
    await switchUser(page, USER_A)
    await page.waitForFunction(() =>
      document.querySelector('.session-rail')?.textContent.includes('还没有已保存的会话'))

    // 1) 首轮发送（真实后端）：请求带 session_id 且不再自证 history；完成后
    //    列表出现会话且服务端已落库 2 条消息
    const firstRequest = page.waitForRequest(req => req.url().endsWith('/api/agent/chat/stream'))
    await send(page, '第一条消息')
    const payload = await (await firstRequest).postDataJSON()
    assert.ok(payload.session_id, '首轮发送应携带服务端会话 id')
    assert.equal(payload.history, undefined, '带会话时上下文由服务端构建，不再发送前端 history')
    await waitDone(page)
    await page.waitForFunction(() => {
      const item = document.querySelector('[data-testid^=session-open-]')
      return item && /第一条消息/.test(item.textContent) && /2 条消息/.test(item.textContent)
    })
    assert.equal(await page.locator('[data-testid^=session-open-]').count(), 1)
    check('first turn persists into the session list with server-built history')

    // 2) 重启恢复：刷新后重新输入用户 ID（前端不持久化用户），列表恢复，
    //    点击恢复已完成对话（历史消息不带运行状态）
    await page.reload({ waitUntil: 'networkidle' })
    await waitConnected(page)
    await switchUser(page, USER_A)
    await page.waitForFunction(() =>
      document.querySelector('[data-testid^=session-open-]'))
    await page.locator('[data-testid^=session-open-]').first().click()
    await page.waitForFunction(() =>
      document.querySelector('.chat-stream')?.textContent.includes('第一条消息'))
    assert.match(await page.locator('.chat-stream').innerText(), /已根据记忆整理回答/)
    assert.equal(await page.getByTestId('run-status').count(), 0,
      '恢复的历史消息不得带运行状态')
    check('restart recovery restores the completed conversation')

    // 3) 用户隔离：B 的列表为空、会话 id 不同；切回 A 后内容互不串扰
    await switchUser(page, USER_B)
    await page.waitForFunction(() =>
      document.querySelector('.session-rail')?.textContent.includes('还没有已保存的会话'))
    const bRequest = page.waitForRequest(req => req.url().endsWith('/api/agent/chat/stream'))
    await send(page, 'B 的消息')
    const bPayload = await (await bRequest).postDataJSON()
    assert.notEqual(bPayload.session_id, payload.session_id, '不同用户的会话必须隔离')
    await waitDone(page)
    await switchUser(page, USER_A)
    await page.waitForFunction(() =>
      document.querySelectorAll('[data-testid^=session-open-]').length === 1)
    assert.match(await page.locator('.session-rail').innerText(), /第一条消息/)
    assert.ok(!(await page.locator('.chat-stream').innerText()).includes('已根据记忆整理回答'),
      '切回后聊天区为空，不得显示其他用户的对话内容')
    check('sessions and in-chat content stay isolated across users')

    // 4) 删除会话：确认后列表移除
    page.on('dialog', (dialog) => dialog.accept())
    await page.locator('.session-delete').first().click()
    await page.waitForFunction(() =>
      document.querySelector('.session-rail')?.textContent.includes('还没有已保存的会话'))
    assert.equal(await page.locator('[data-testid^=session-open-]').count(), 0)
    check('deleting a session removes it from the rail')

    // 5) 中断不冒充完成：EOF 回合不落库，重启后聊天区不出现该轮内容
    await page.route('**/api/agent/chat/stream', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    })
    await send(page, 'EOF 消息')
    await page.getByTestId('run-failure').waitFor()
    assert.match(await page.getByTestId('run-failure').innerText(), /连接提前结束/)
    await page.reload({ waitUntil: 'networkidle' })
    await waitConnected(page)
    await switchUser(page, USER_A)
    assert.ok(!(await page.locator('.chat-stream').innerText()).includes('EOF 消息'),
      '中断回合不得在重启后冒充完成')
    await page.waitForFunction(() => {
      const item = document.querySelector('[data-testid^=session-open-]')
      return item && /0 条消息/.test(item.textContent)
    }, undefined, { timeout: 8000 }).catch(() => {})
    const leftover = page.locator('[data-testid^=session-open-]').first()
    if (await leftover.count()) {
      await leftover.click()
      await page.waitForFunction(() =>
        !(document.querySelector('.chat-stream')?.textContent.includes('EOF 消息')))
    }
    await page.unroute('**/api/agent/chat/stream')
    check('interrupted turn is never restored as a completed conversation')

    console.log('All session browser checks passed.')
  } catch (error) {
    await captureFailure(page, 'session', error)
    throw error
  } finally {
    await browser.close()
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
