// Browser regression for chat terminal states and the safe-forget flow.
// Runs against Vite plus the isolated API fixture; no external model is used.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const assert = require('node:assert/strict')
const { captureFailure, launchOptions } = require('./browser-artifacts.cjs')

const base = new URL(process.env.FRONTEND_TEST_URL || 'http://127.0.0.1:5174')
base.searchParams.set('view', 'chat')

const frame = (event) => `data: ${JSON.stringify(event)}\n\n`
const check = (name) => console.log('PASS', name)
const startNewChat = async (page) => {
  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForFunction(() => document.querySelector('[data-testid=connection-button]')?.textContent.includes('已连接'))
}

;(async () => {
  const browser = await chromium.launch(launchOptions())
  let page = null
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    page.setDefaultTimeout(15000)
    await page.goto(base.href, { waitUntil: 'networkidle' })
    await page.waitForFunction(() => document.querySelector('[data-testid=connection-button]')?.textContent.includes('已连接'))

    await page.route('**/api/agent/chat/stream', async route => {
      const message = route.request().postDataJSON()?.message || ''
      if (message.includes('EOF')) {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
      } else if (message.includes('错误')) {
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: frame({ type: 'error', message: '模型失败', workflow: { status: 'failed' } }),
        })
      } else {
        await route.continue()
      }
    })

    await page.getByTestId('chat-input').fill('正常完成')
    await page.getByTestId('chat-send').click()
    await page.waitForFunction(() => [...document.querySelectorAll('[data-testid=run-status]')]
      .some(element => /已完成/.test(element.textContent || '')))
    assert.match(await page.locator('.chat-stream').innerText(), /已根据记忆整理回答/)
    check('chat done renders the final answer')

    await startNewChat(page)
    await page.getByTestId('chat-input').fill('触发错误')
    await page.getByTestId('chat-send').click()
    await page.getByTestId('run-failure').waitFor()
    assert.match(await page.getByTestId('run-failure').innerText(), /模型失败/)
    assert.match(await page.getByTestId('run-status').innerText(), /执行失败/)
    check('chat error keeps a visible failure state')

    await startNewChat(page)
    await page.getByTestId('chat-input').fill('触发 EOF')
    await page.getByTestId('chat-send').click()
    await page.getByTestId('run-failure').waitFor()
    assert.match(await page.getByTestId('run-failure').innerText(), /连接提前结束/)
    check('chat EOF is not treated as success')

    await startNewChat(page)
    const ask = async (message) => {
      const request = page.waitForRequest(req => req.url().endsWith('/api/agent/chat/stream'))
      await page.getByTestId('chat-input').fill(message)
      await page.getByTestId('chat-send').click()
      return (await request).postDataJSON()
    }
    const first = await ask('本轮上下文标记')
    assert.ok(first.session_id, '首轮发送应已创建服务端会话（TASK-018）')
    const beforeForget = await ask('检查是否有前文')
    assert.equal(beforeForget.session_id, first.session_id)
    assert.equal(beforeForget.history, undefined, '带会话时上下文由服务端构建，前端不再自证 history')
    await page.locator('#dock-tab-memory').click()
    await page.getByTestId('tab-forget').click()
    await page.getByTestId('forget-input').fill('清理测试记忆')
    const forgetResponse = page.waitForResponse(res => res.url().endsWith('/api/forget'))
    await page.getByTestId('forget-submit').click()
    assert.equal((await forgetResponse).status(), 200)
    await page.waitForFunction(() => document.querySelector('[data-testid=forget-result]')?.textContent.includes('删除数量：0'))
    assert.match(await page.getByTestId('forget-result').innerText(), /清理测试记忆/)
    const afterForget = await ask('检查遗忘后的上下文')
    assert.ok(afterForget.session_id && afterForget.session_id !== first.session_id,
      '遗忘后新对话使用新的服务端会话')
    assert.equal(afterForget.history, undefined, '遗忘后同样不重发前端 history')
    check('forget flow reports the backend result and clears chat history')

    console.log('All chat browser checks passed.')
  } catch (error) {
    await captureFailure(page, 'chat-flow', error)
    throw error
  } finally {
    await browser.close()
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
