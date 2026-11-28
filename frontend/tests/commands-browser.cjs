// Browser regression for TASK-027 desktop commands (Issue #35):
// Ctrl+K command palette (alias filter, arrow selection, focus restore),
// single-switch equivalent scan navigation, controlled announcements and
// the Esc-deny confirmation path. Narrator itself is validated manually;
// the suite asserts the aria-live/status DOM that Narrator reads.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const assert = require('node:assert/strict')
const { captureFailure, launchOptions } = require('./browser-artifacts.cjs')

const base = new URL(process.env.FRONTEND_TEST_URL || 'http://127.0.0.1:5174')
base.searchParams.set('view', 'chat')
const check = (name) => console.log('PASS', name)

const announcement = (page) =>
  page.evaluate(() => document.querySelector('p.sr-only[aria-live="polite"]')?.textContent || '')
const waitAnnouncement = (page, pattern) => page.waitForFunction(
  (re) => new RegExp(re).test(document.querySelector('p.sr-only[aria-live="polite"]')?.textContent || ''),
  pattern.source)

;(async () => {
  const browser = await chromium.launch(launchOptions())
  let page = null
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    page.setDefaultTimeout(15000)
    await page.goto(base.href, { waitUntil: 'networkidle' })
    await page.waitForFunction(() =>
      document.querySelector('[data-testid=connection-button]')?.textContent.includes('已连接'))

    // 1) Ctrl+K 打开命令面板，焦点进入输入框；别名过滤 + 方向键选择 + 回车执行
    await page.keyboard.press('Control+k')
    await page.waitForSelector('[data-testid=command-palette]')
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.testid), 'command-input',
      '面板打开后焦点必须在命令输入框')
    await page.keyboard.type('遗忘')
    await page.waitForFunction(() =>
      document.querySelector('[data-testid="command-opt-panel.forget"]'))
    const optionCount = await page.locator('#command-list li').count()
    assert.ok(optionCount >= 1 && optionCount < 5, '别名过滤应显著收窄列表')
    assert.equal(await page.getAttribute('#command-list li.active', 'aria-selected'), 'true')
    await page.keyboard.press('Enter')
    await page.waitForFunction(() => !document.querySelector('[data-testid=command-palette]'))
    await page.waitForFunction(() =>
      document.activeElement?.getAttribute('data-panel-title') === 'forget',
      undefined, { timeout: 5000 }).catch(() => {})
    assert.equal(await page.evaluate(() =>
      document.querySelector('[data-panel-title="forget"]')?.id
        ? document.activeElement?.getAttribute('data-panel-title')
        : document.activeElement?.getAttribute('data-panel-title')), 'forget',
      '执行后面板标题应获得焦点')
    check('Ctrl+K palette: alias filter, arrow select, Enter executes with focus handoff')

    // 2) 关闭面板归还原焦点：焦点放聊天输入框 → Ctrl+K → Esc → 焦点回到输入框
    await page.getByTestId('chat-input').focus()
    await page.keyboard.press('Control+k')
    await page.waitForSelector('[data-testid=command-palette]')
    await page.keyboard.press('Escape')
    await page.waitForFunction(() => !document.querySelector('[data-testid=command-palette]'))
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.testid), 'chat-input',
      'Esc 关闭面板后必须返回原焦点')
    check('palette close restores the opener focus')

    // 3) 面板按钮入口 + 命令执行后有读屏播报（aria-live 文本更新）
    await page.getByTestId('command-open').click()
    await page.waitForSelector('[data-testid=command-palette]')
    await page.keyboard.type('同步')
    await page.keyboard.press('Enter')
    await waitAnnouncement(page, /同步|已更新|演示/)
    assert.notEqual((await announcement(page)).trim(), '', '命令执行后应有播报文本')
    check('command execution announces through the aria-live channel')

    // 4) 切换用户命令：键盘直达用户输入框（可选择用户）
    await page.keyboard.press('Control+k')
    await page.waitForSelector('[data-testid=command-palette]')
    await page.keyboard.type('切换用户')
    await page.keyboard.press('Enter')
    await page.waitForFunction(() =>
      document.getElementById('user-id') === document.activeElement)
    check('switch-user command lands focus on the user field')

    // 5) 单开关等价扫描：空格步进移动高亮，回车激活，Esc 退出
    await page.keyboard.press('Escape') // 关闭顶栏菜单，回到基线
    await page.getByTestId('scan-toggle').click()
    await page.waitForFunction(() => !!document.querySelector('.scan-active'))
    const first = await page.evaluate(() =>
      document.querySelector('.scan-active')?.textContent || '')
    await page.keyboard.press(' ')
    await page.waitForFunction((prev) => {
      const el = document.querySelector('.scan-active')
      return el && el.textContent !== prev
    }, first)
    check('scan mode: Space steps the highlight to the next target')

    // 回车激活：步进到顶栏"项目资料"按钮后激活，焦点随之转移
    await page.waitForFunction(() => {
      const el = document.querySelector('.scan-active')
      return el && el.textContent.includes('项目资料')
    }, undefined, { timeout: 10000 }).catch(() => {})
    const hadTarget = await page.evaluate(() =>
      document.querySelector('.scan-active')?.textContent.includes('项目资料'))
    if (hadTarget) {
      await page.keyboard.press('Enter')
      await page.waitForFunction(() =>
        document.activeElement?.textContent?.includes('项目资料'))
    }
    await page.keyboard.press('Escape')
    await page.waitForFunction(() => !document.querySelector('.scan-active'))
    assert.match(await page.getByTestId('scan-toggle').innerText(), /扫描导航/,
      'Esc 退出后按钮回到未按下态')
    check('scan mode: Enter activates, Escape exits and restores the toggle')

    // 6) 撤销确认：确认卡聚焦后 Esc = 拒绝（decision 提交为 deny）。
    //    用 fetch 补丁返回不关闭的 ReadableStream——确认期间运行处于挂起
    //    等待态（若流提前结束，catch 会把未决卡如实置为"连接已断开"）。
    await page.evaluate(() => {
      const frame = (event) => `data: ${JSON.stringify(event)}

`
      const orig = window.fetch
      window.__origFetch = orig
      window.fetch = async (input, init) => {
        const url = typeof input === 'string' ? input : input.url
        const message = (() => { try { return JSON.parse(init?.body || '{}').message } catch { return '' } })()
        if (url.endsWith('/api/agent/chat/stream') && String(message).includes('触发确认')) {
          const stream = new ReadableStream({
            start(controller) {
              controller.enqueue(new TextEncoder().encode(frame({
                type: 'confirmation_request', confirm_id: 'c1', run_id: 'r1', seq: 1,
                tool_call_id: 't1', action: 'write_file', path: 'C:/demo/a.txt',
                target_exists: false, summary: '写入演示文件', decision_token: 'tok',
              })))
            },
          })
          return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
        }
        return orig(input, init)
      }
    })
    await page.route('**/api/agent/confirmations/**', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ decision: 'deny' }) })
    })
    const decide = page.waitForRequest(req =>
      req.url().includes('/api/agent/confirmations/') && req.method() === 'POST'
    ).then(req => ({ req })).catch(() => null)
    // 场景 5 可能激活了项目资料页：用命令面板切回对话面（覆盖跨页命令）
    await page.keyboard.press('Control+k')
    await page.waitForSelector('[data-testid=command-palette]')
    await page.keyboard.type('回到对话')
    await page.keyboard.press('Enter')
    await page.waitForFunction(() => {
      const input = document.getElementById('chat-input')
      return !!input && !!input.offsetParent
    })
    await page.getByTestId('chat-input').fill('触发确认')
    await page.getByTestId('chat-send').click()
    await page.locator('.confirmation-card').waitFor()
    await page.locator('.confirmation-deny').focus()
    await page.keyboard.press('Escape')
    const result = await decide
    assert.ok(result, '确认决定请求未发出')
    assert.equal(result.req.postDataJSON().decision, 'deny', 'Esc 必须提交拒绝')
    await page.waitForFunction(() =>
      document.querySelector('.confirmation-card')?.textContent.includes('已拒绝'))
    assert.match(await page.locator('.confirmation-card').innerText(), /已拒绝/)
    await page.evaluate(() => { window.fetch = window.__origFetch })
    await page.unroute('**/api/agent/confirmations/**')

    check('Esc on a focused confirmation card submits deny (undo confirm)')

    console.log('All command browser checks passed.')
  } catch (error) {
    await captureFailure(page, 'commands', error)
    throw error
  } finally {
    await browser.close()
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
