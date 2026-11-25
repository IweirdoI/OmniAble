// Browser interaction regression for TASK-012. Run against Vite and the isolated
// fixture API (frontend/tests/serve_api.py):
//
//   PLAYWRIGHT_MODULE=/path/to/playwright node frontend/tests/workbench-browser.cjs
//
// PLAYWRIGHT_MODULE can point at an existing Playwright installation. Windows
// defaults to Edge; other platforms use Playwright Chromium.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const assert = require('node:assert/strict')
const { captureFailure, launchOptions } = require('./browser-artifacts.cjs')

const base = new URL(process.env.FRONTEND_TEST_URL || 'http://127.0.0.1:5174')
base.searchParams.set('view', 'chat')
const check = (name) => console.log('PASS', name)
const noOverflow = async (page, label) => {
  const size = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, width: innerWidth }))
  assert.equal(size.scrollWidth, size.width, `${label}: horizontal overflow`)
}
const activeTestId = async (page) => page.evaluate(() => document.activeElement?.dataset?.testid || '')
const waitForLive = async (page) => {
  await page.getByTestId('connection-button').waitFor()
  await page.waitForFunction(() => document.querySelector('[data-testid=connection-button]')?.textContent.includes('已连接'))
}

;(async () => {
  const browser = await chromium.launch(launchOptions())
  const errors = []
  let page = null
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
    page = await context.newPage()
    const profileRequests = []
    page.setDefaultTimeout(15000)
    page.on('request', request => {
      const url = new URL(request.url())
      if (url.pathname.endsWith('/profile')) profileRequests.push(url)
    })
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (message.type() === 'error') errors.push('console: ' + message.text()) })
    await page.goto(base.href, { waitUntil: 'networkidle' })
    await waitForLive(page)
    await noOverflow(page, '1440')

    const columns = await page.evaluate(() => ({
      rail: document.querySelector('.session-rail')?.getBoundingClientRect().width,
      chat: document.querySelector('.chat-panel')?.getBoundingClientRect().width,
      panel: document.querySelector('.memory-panel')?.getBoundingClientRect().width,
    }))
    assert(columns.rail >= 220 && columns.chat > 600 && columns.panel >= 360, 'three-region desktop layout')
    check('1440: rail, chat and memory panel share the workbench without page overflow')

    // TASK-013: scene selection must reach chat/profile requests and isolate history.
    await page.waitForFunction(() => document.querySelector('[data-testid=sync-profile]')?.className.includes('state-success'))
    assert.equal(await page.getByTestId('scene-select').inputValue(), 'global')
    assert.equal(await page.getByTestId('scene-select').locator('option').count(), 3)
    assert(profileRequests.some(url => url.searchParams.get('global_only') === 'true'))

    const codingProfile = page.waitForRequest(request => {
      const url = new URL(request.url())
      return url.pathname.endsWith('/profile') && url.searchParams.get('scene_id') === 'coding'
    })
    await page.getByTestId('scene-select').selectOption('coding')
    await codingProfile
    await page.waitForFunction(() => !document.querySelector('[data-testid=scene-select]').disabled)

    let sceneChatPayload = null
    await page.route('**/api/agent/chat/stream', async route => {
      sceneChatPayload = route.request().postDataJSON()
      const event = {
        type: 'done', run_id: 'scene-browser', seq: 1,
        memories: [], recall_ms: 1, latency_ms: 2,
        workflow: { status: 'completed' },
      }
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: `data: ${JSON.stringify(event)}\n\n`,
      })
    })
    await page.getByTestId('chat-input').fill('场景切换验证')
    await page.getByTestId('chat-send').click()
    await page.waitForFunction(() => [...document.querySelectorAll('[data-testid=run-status]')]
      .some(element => /已完成/.test(element.textContent || '')))
    assert.equal(sceneChatPayload?.scene_id, 'coding')
    await page.waitForFunction(() => !document.querySelector('[data-testid=scene-select]').disabled)

    const officeProfile = page.waitForRequest(request => {
      const url = new URL(request.url())
      return url.pathname.endsWith('/profile') && url.searchParams.get('scene_id') === 'office'
    })
    await page.getByTestId('scene-select').selectOption('office')
    await officeProfile
    await page.waitForFunction(() => document.querySelectorAll('.message-row.user').length === 0)
    assert.equal(await page.getByTestId('scene-select').inputValue(), 'office')
    assert.doesNotMatch(await page.locator('.chat-stream').innerText(), /场景切换验证/)
    check('scene selection scopes chat/profile requests and clears previous chat history')
    await page.unroute('**/api/agent/chat/stream')

    await page.locator('#dock-tab-memory').press('ArrowLeft')
    assert.equal(await page.locator('#dock-tab-workspace').getAttribute('aria-selected'), 'true')
    await page.locator('#dock-tab-workspace').press('ArrowRight')
    assert.equal(await page.locator('#dock-tab-memory').getAttribute('aria-selected'), 'true')
    await page.locator('#dock-tab-memory').press('ArrowRight')
    assert.equal(await page.locator('#dock-tab-plugins').getAttribute('aria-selected'), 'true')
    await page.locator('#dock-tab-plugins').press('ArrowLeft')
    const panelBefore = await page.locator('.memory-panel').evaluate(element => element.getBoundingClientRect().width)
    await page.locator('.dock-expand').click()
    await page.waitForFunction(previous => document.querySelector('.memory-panel').getBoundingClientRect().width > previous, panelBefore)
    const panelWide = await page.locator('.memory-panel').evaluate(element => element.getBoundingClientRect().width)
    assert(panelWide > panelBefore, 'expanded panel should be wider')
    await page.locator('.dock-expand').click()
    await page.waitForFunction(previous => document.querySelector('.memory-panel').getBoundingClientRect().width <= previous, panelBefore)
    check('workspace, memory and plugin tabs support arrow navigation and width expansion')

    await page.locator('#dock-tab-plugins').click()
    await page.locator('[data-panel-title=plugins]').waitFor()
    assert.equal(await page.locator('.plugin-card').count(), 3)
    await page.getByTestId('plugin-card-chat-export').getByRole('button', { name: '安装' }).click()
    await page.waitForFunction(() => document.querySelector('[data-testid=plugin-card-chat-export] button.plugin-toggle')?.textContent.includes('移除'))
    await page.getByTestId('plugin-card-chat-export').getByRole('button', { name: '打开' }).click()
    await page.waitForFunction(() => document.getElementById('plugin-detail-title') === document.activeElement)
    assert.equal(await page.locator('#plugin-detail-title').innerText(), '对话导出')
    const downloadPromise = page.waitForEvent('download')
    await page.getByTestId('plugin-export').click()
    const download = await downloadPromise
    assert.match(download.suggestedFilename(), /^omnidable-chat-.*\.md$/)
    await page.locator('.plugin-back').click()
    await page.waitForFunction(() => document.querySelector('[data-panel-title=plugins]') === document.activeElement)
    await page.getByTestId('plugin-card-quick-prompts').getByRole('button', { name: '打开' }).click()
    await page.getByTestId('plugin-prompt').first().click()
    assert.match(await page.getByTestId('chat-input').inputValue(), /偏好/)
    assert.equal(await page.evaluate(() => document.activeElement?.dataset?.testid), 'chat-input')
    check('plugins can be installed, opened, run and exported locally')

    await page.locator('#dock-tab-memory').click()
    await page.getByTestId('tab-preferences').click()
    assert.equal(await page.getByTestId('tab-preferences').getAttribute('aria-selected'), 'true')
    await page.getByTestId('tab-preferences').press('ArrowRight')
    assert.equal(await page.getByTestId('tab-memories').getAttribute('aria-selected'), 'true')
    assert.equal(await page.locator('#memory-content').getAttribute('aria-labelledby'), 'tab-memories')
    check('memory tabs expose selection and update with arrow keys')

    await page.getByTestId('chat-input').fill('同用户导航保留的草稿')
    await page.getByTestId('open-ingestion').click()
    await page.waitForFunction(() => document.querySelector('[data-panel-title=ingestion]') === document.activeElement)
    await page.getByTestId('context-close').click()
    await page.waitForFunction(() => document.activeElement?.dataset?.testid === 'open-ingestion')
    assert.equal(await page.getByTestId('chat-input').inputValue(), '同用户导航保留的草稿')
    check('same-user panel navigation keeps the draft and returns focus to the opener')

    await page.getByTestId('chat-input').fill('切用户后应被清除')
    const uid = 'ui_switch_' + Date.now()
    await page.getByTestId('user-input').fill(uid)
    await page.getByTestId('user-switch').click()
    await page.waitForFunction(expected => document.querySelector('[data-testid=active-user]')?.textContent === expected, uid)
    await page.waitForFunction(() => !document.querySelector('[data-testid=sync-button]').disabled)
    assert.equal(await page.getByTestId('chat-input').inputValue(), '')
    assert.equal(await page.getByTestId('run-status').count(), 0)
    assert.match(await page.locator('.chat-stream').innerText(), /OmniAble 助理/)
    check('switching users clears the draft and previous conversation state')

    await page.getByTestId('user-input').fill('not-switched')
    await page.getByTestId('sync-button').click()
    await page.waitForTimeout(150)
    assert.equal(await page.evaluate(() => document.querySelector('[data-testid=active-user]').textContent), uid)
    assert.match(await page.locator('.toast').innerText(), /先点“切换用户”/)
    await page.getByTestId('user-input').fill(uid)
    check('sync refreshes only the active user instead of silently switching')

    await page.locator('.panel-toggle').click()
    await page.getByTestId('tab-overview').click()
    await page.getByTestId('chat-input').fill('键盘顺序')
    await page.getByTestId('chat-input').focus()
    await page.keyboard.press('Tab')
    assert.equal(await activeTestId(page), 'open-ingestion')
    await page.keyboard.press('Tab')
    assert.equal(await activeTestId(page), 'chat-send')
    const focusStyle = await page.evaluate(() => ({
      width: getComputedStyle(document.activeElement).outlineWidth,
      style: getComputedStyle(document.activeElement).outlineStyle,
    }))
    assert.equal(focusStyle.width, '2px')
    assert.equal(focusStyle.style, 'solid')
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const reducedDuration = await page.evaluate(() => parseFloat(getComputedStyle(document.querySelector('.send-button')).transitionDuration))
    assert(reducedDuration <= 0.01, 'reduced motion should suppress transitions')
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    check('composer tab order reaches controls with a visible focus ring and reduced motion')
    await page.locator('.panel-toggle').click()

    await page.setViewportSize({ width: 390, height: 844 })
    await noOverflow(page, '390')
    await page.getByTestId('chat-input').fill('手机端保留草稿')
    await page.getByRole('button', { name: '打开菜单' }).click()
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'mobile-menu-title')
    assert.equal(await page.locator('#workspace-menu').getAttribute('role'), 'dialog')
    await page.keyboard.press('Shift+Tab')
    assert.match(await page.evaluate(() => document.activeElement?.textContent || ''), /返回聊天/)
    await page.locator('.mobile-menu-actions').getByRole('button', { name: /记忆总览/ }).click()
    await page.waitForFunction(() => document.querySelector('[data-panel-title=overview]') === document.activeElement)
    await noOverflow(page, '390 memory panel')
    assert.equal(await page.getByTestId('chat-input').inputValue(), '手机端保留草稿')
    await page.getByRole('button', { name: '返回聊天' }).click()
    await page.waitForFunction(() => document.activeElement?.classList.contains('chat-title'))
    assert.equal(await page.getByTestId('chat-input').inputValue(), '手机端保留草稿')
    check('mobile menu opens with focus, panel back returns to chat with draft intact')

    await page.getByRole('button', { name: '打开菜单' }).click()
    await page.keyboard.press('Escape')
    assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('mobile-menu-button')), true)
    check('Escape closes the mobile menu and returns focus to its trigger')

    await page.getByRole('button', { name: '打开菜单' }).click()
    const mobileUid = 'ui_mobile_' + Date.now()
    await page.getByTestId('user-input').fill(mobileUid)
    await page.getByTestId('user-switch').click()
    await page.waitForFunction(expected => document.querySelector('[data-testid=active-user]')?.textContent === expected, mobileUid)
    await page.waitForFunction(() => !document.querySelector('[data-testid=sync-button]').disabled)
    assert.equal(await page.getByTestId('chat-input').inputValue(), '')
    check('mobile user switch clears the previous draft')

    await page.getByTestId('sync-button').click()
    await page.waitForFunction(() => !document.querySelector('[data-testid=sync-button]').disabled)
    assert.match(await page.locator('.toast').innerText(), /记忆数据已更新/)
    check('mobile menu sync refreshes the active user in place')

    await page.locator('.mobile-menu-actions').getByRole('button', { name: /接入/ }).click()
    await page.waitForFunction(() => document.querySelector('[data-panel-title=ingestion]') === document.activeElement)
    await noOverflow(page, '390 ingestion panel')
    await page.getByRole('button', { name: '返回聊天' }).click()
    await page.waitForFunction(() => document.activeElement?.classList.contains('chat-title'))
    await page.getByRole('button', { name: '打开菜单' }).click()
    await page.locator('.mobile-menu-actions').getByRole('button', { name: /遗忘/ }).click()
    await page.waitForFunction(() => document.querySelector('[data-panel-title=forget]') === document.activeElement)
    assert.equal(await page.getByLabel('需要遗忘的内容').count(), 1)
    await page.getByRole('button', { name: '返回聊天' }).click()
    await page.waitForFunction(() => document.activeElement?.classList.contains('chat-title'))
    await page.getByRole('button', { name: '打开菜单' }).click()
    await page.locator('.mobile-menu-actions').getByRole('button', { name: /工作区/ }).click()
    await page.waitForFunction(() => document.getElementById('workspace-panel-title') === document.activeElement)
    await page.getByRole('button', { name: '返回聊天' }).click()
    await page.waitForFunction(() => document.activeElement?.classList.contains('chat-title'))
    await page.getByRole('button', { name: '打开菜单' }).click()
    await page.locator('.mobile-menu-actions').getByRole('button', { name: /插件/ }).click()
    await page.waitForFunction(() => document.querySelector('[data-panel-title=plugins]') === document.activeElement)
    await noOverflow(page, '390 plugin panel')
    assert.equal(await page.locator('.plugin-card').count(), 3)
    await page.getByRole('button', { name: '返回聊天' }).click()
    await page.waitForFunction(() => document.activeElement?.classList.contains('chat-title'))
    check('mobile menu reaches ingestion, forget, workspace and plugins and returns focus to chat')

    await page.getByRole('button', { name: '打开菜单' }).click()
    await page.locator('.menu-close').click()
    assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('mobile-menu-button')), true)
    check('mobile menu closes back to its trigger')

    await page.getByTestId('chat-input').focus()
    await page.setViewportSize({ width: 390, height: 360 })
    await page.waitForTimeout(120)
    const composer = await page.evaluate(() => {
      const box = document.querySelector('.chat-composer').getBoundingClientRect()
      const send = document.querySelector('[data-testid=chat-send]').getBoundingClientRect()
      return { bottom: box.bottom, sendBottom: send.bottom, height: innerHeight }
    })
    assert(composer.bottom <= composer.height + 1, 'composer moves above the reduced visual viewport')
    assert(composer.sendBottom <= composer.height + 1, 'send button remains reachable')
    check('390 viewport with simulated keyboard inset keeps composer and send reachable')

    await page.setViewportSize({ width: 768, height: 1024 })
    await page.getByTestId('chat-input').fill('')
    await noOverflow(page, '768')
    await page.getByRole('button', { name: '打开菜单' }).click()
    await page.locator('.menu-close').click()
    check('768 uses the compact shell without horizontal overflow')

    await page.setViewportSize({ width: 720, height: 450 })
    await noOverflow(page, '720/200%')
    await page.getByTestId('chat-input').fill('200% 缩放检查')
    const zoomLayout = await page.evaluate(() => {
      const box = document.querySelector('.chat-composer').getBoundingClientRect()
      const send = document.querySelector('[data-testid=chat-send]').getBoundingClientRect()
      return { bottom: box.bottom, sendBottom: send.bottom, height: innerHeight }
    })
    assert(zoomLayout.bottom <= zoomLayout.height + 1 && zoomLayout.sendBottom <= zoomLayout.height + 1)
    check('1440 at 200% (720 CSS px) keeps composer and send visible')

    await page.setViewportSize({ width: 1440, height: 900 })
    await page.getByTestId('connection-button').click()
    await page.waitForFunction(() => document.querySelector('[data-testid=connection-button]')?.textContent.includes('演示'))
    const longCode = Array.from({ length: 12 }, (_, index) => 'const value' + index + ' = "' + 'x'.repeat(220) + '";').join('\n')
    await page.getByTestId('chat-input').fill('```js\n' + longCode + '\n```')
    await page.getByTestId('chat-send').click()
    await page.locator('.bubble pre').waitFor()
    await noOverflow(page, 'long code')
    const codeBox = await page.locator('.bubble pre').evaluate(element => ({ scrollWidth: element.scrollWidth, clientWidth: element.clientWidth }))
    assert(codeBox.scrollWidth > codeBox.clientWidth, 'long code should scroll locally')
    check('long code scrolls inside the message without page overflow')

    await page.getByTestId('chat-input').fill('长文本检查' + '很长'.repeat(120))
    await page.getByTestId('chat-send').click()
    await page.waitForTimeout(550)
    await noOverflow(page, 'long text')
    const textBox = await page.locator('.message-row.user .bubble').last().evaluate(element => ({ scrollWidth: element.scrollWidth, clientWidth: element.clientWidth }))
    assert(textBox.scrollWidth <= textBox.clientWidth + 1, 'long Chinese text should wrap instead of widening the page')
    check('long Chinese text wraps inside the message column')

    const composerLabel = await page.getByLabel('对话输入').count()
    const userLabel = await page.getByLabel('用户 ID').count()
    assert.equal(composerLabel, 1)
    assert.equal(userLabel, 1)
    assert(await page.locator('[aria-live="polite"]').count() > 0)
    const duplicateIds = await page.evaluate(() => {
      const ids = [...document.querySelectorAll('[id]')].map(element => element.id)
      return ids.filter((id, index) => ids.indexOf(id) !== index)
    })
    assert.deepEqual(duplicateIds, [])
    check('composer and user inputs keep explicit labels, unique ids and status announcements')

    const retryPage = await context.newPage()
    const retryUid = 'ui_retry_' + Date.now()
    let failProfileFor = retryUid
    await retryPage.route('**/api/preferences/*/profile*', async route => {
      if (failProfileFor && route.request().url().includes(failProfileFor)) {
        failProfileFor = null
        await route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"temporary"}' })
      } else {
        await route.continue()
      }
    })
    await retryPage.setViewportSize({ width: 1440, height: 900 })
    await retryPage.goto(base.href, { waitUntil: 'networkidle' })
    await waitForLive(retryPage)
    await retryPage.getByTestId('user-input').fill(retryUid)
    await retryPage.getByTestId('user-switch').click()
    await retryPage.waitForFunction(expected => document.querySelector('[data-testid=active-user]')?.textContent === expected, retryUid)
    await retryPage.waitForFunction(() => document.querySelector('[data-testid=sync-profile]')?.classList.contains('state-error'))
    await retryPage.getByTestId('tab-preferences').click()
    await retryPage.locator('.stale-banner').waitFor()
    assert.match(await retryPage.locator('[data-panel-title=preferences]').innerText(), /未确认/)
    await retryPage.locator('.stale-banner').getByRole('button', { name: '重试偏好' }).click()
    await retryPage.waitForFunction(() => !document.querySelector('.stale-banner') && document.querySelector('[data-testid=sync-profile]')?.classList.contains('state-success'))
    check('a failed panel keeps its own retry and clears the stale banner after success')
    await retryPage.close()

    assert.deepEqual(errors, [])
    console.log('All workbench browser checks passed.')
  } catch (error) {
    await captureFailure(page, 'workbench', error)
    throw error
  } finally {
    await browser.close()
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
