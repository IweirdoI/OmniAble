// Run against Vite and the isolated API from serve_api.py.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const { captureFailure, launchOptions } = require('./browser-artifacts.cjs')

const base = process.env.FRONTEND_TEST_URL || 'http://127.0.0.1:5186'
const artifacts = process.argv[2]
const button = (page, name) => page.getByRole('button', { name, exact: true })
const attach = (page, name, content) => page.locator('.project-form input[type=file]').setInputFiles({
  name, mimeType: 'text/plain', buffer: Buffer.from(content),
})
const noOverflow = async page => assert(await page.evaluate(() =>
  document.querySelector('.project-home').scrollWidth <= innerWidth && document.documentElement.scrollWidth <= innerWidth))

;(async () => {
  const browser = await chromium.launch(launchOptions())
  const errors = []
  let page = null
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    page.setDefaultTimeout(9000)
    page.on('pageerror', error => errors.push(error.message))
    await page.goto(base)
    await page.getByRole('heading', { name: '项目资料', exact: true }).waitFor()
    assert.equal(await page.getByTestId('chat-input').isVisible(), false)
    await button(page, '创建第一个项目').click()
    await page.getByLabel('项目名').fill('发布验收')
    await button(page, '创建项目').click()

    async function addFile(name, content) {
      await button(page, '＋ 添加文件').click()
      await page.getByLabel('归入目录').fill('交付')
      await attach(page, name, content)
      await button(page, '保存文件').click()
      await page.getByRole('heading', { name, exact: true }).waitFor()
    }
    await addFile('private.md', 'Keep this unselected file private.')
    await addFile('release.md', 'Deprecated release plan.')
    await button(page, '整理知识卡').click()
    await page.getByLabel('知识标题').fill('验收步骤')
    await page.getByLabel('知识正文').fill('原验收流程。')
    await button(page, '保存知识卡').click()
    await button(page, '查看来源（v1）').click()
    await button(page, '新增版本').click()
    await attach(page, 'release.md', 'Approved release plan.')
    await button(page, '保存文件').click()
    await page.getByText('Approved release plan.', { exact: true }).waitFor()
    await button(page, '移动到目录').click()
    await page.getByLabel('目标目录').fill('发布')
    await button(page, '移动文件').click()
    await page.getByText('已移动文件。').waitFor()
    await button(page, '验收步骤').click()
    assert.equal(await button(page, '带到对话').isVisible(), false)
    await page.locator('.project-detail').getByText('来源有更新，待复核').waitFor()
    await button(page, '编辑／复核').click()
    await button(page, '改用最新版').click()
    await page.getByLabel('知识正文').fill('执行正式验收流程。')
    await button(page, '保存知识卡').click()
    await button(page, '查看来源（v2）').click()
    await noOverflow(page)
    if (artifacts) await page.screenshot({ path: path.join(artifacts, 'project-library-desktop.png') })
    console.log('PASS project-first landing, folders, file versions and knowledge review')

    await button(page, '共享文件').click()
    await page.getByRole('checkbox', { name: '发布 / release.md' }).check()
    const download = page.waitForEvent('download')
    await button(page, '下载分享包').click()
    const archive = await (await download).path()
    const bundle = await fs.readFile(archive, 'utf8')
    assert.match(bundle, /Approved release plan/)
    assert.doesNotMatch(bundle, /private\.md|Deprecated release plan|原验收流程/)

    const teammate = await browser.newPage({ viewport: { width: 390, height: 844 } })
    teammate.setDefaultTimeout(9000)
    teammate.on('pageerror', error => errors.push(error.message))
    await teammate.goto(base)
    await button(teammate, '创建第一个项目').waitFor()
    await button(teammate, '导入分享包').click()
    await attach(teammate, 'invalid.omniable.json', '{invalid')
    await button(teammate, '预览分享包').click()
    await teammate.getByRole('alert').getByText('分享包不是有效的 JSON 文件。').waitFor()
    await attach(teammate, 'release.omniable.json', bundle)
    await button(teammate, '预览分享包').click()
    await teammate.getByText(/包含 1 份文件、1 张知识卡/).waitFor()
    await button(teammate, '确认导入').click()
    await teammate.getByRole('button', { name: /release\.md/ }).click()
    await teammate.getByText('Approved release plan.', { exact: true }).waitFor()
    await button(teammate, '知识页').click()
    await teammate.getByRole('button', { name: /验收步骤/ }).click()
    await button(teammate, '查看来源（v2）').click()
    await noOverflow(teammate)
    if (artifacts) await teammate.screenshot({ path: path.join(artifacts, 'project-library-mobile.png') })
    await teammate.reload()
    await teammate.getByRole('button', { name: /release\.md/ }).waitFor()
    console.log('PASS explicitly selected share package, independent import, persistence and mobile layout')

    await page.getByTestId('connection-button').filter({ hasText: '已连接' }).waitFor()
    // Inject one entry and an isolated API error to check the lifecycle views and retry.
    await page.route('**/api/short-term/**', route => route.fulfill({ json: { count: 1, items: [{
      id: 'memory-preview-example', memory_type: 'knowledge', content: '验收会议安排',
      version: 2, metadata: { filename: 'agenda.md' }, created_at: '2026-11-24T09:00:00',
    }] } }))
    let failMid = true
    await page.route('**/api/mid-term/**', route => failMid
      ? route.fulfill({ status: 503, json: { detail: 'temporary error' } }) : route.continue())
    await button(page, '记忆流转').click()
    await button(page, '重试中期').waitFor()
    await page.getByRole('button', { name: /验收会议安排/ }).click()
    await page.getByRole('region', { name: '记忆详情' }).getByText('agenda.md').waitFor()
    await button(page, '分层泳道').click()
    await page.getByRole('region', { name: '长期记忆' }).waitFor()
    failMid = false
    await button(page, '重试中期').click()
    await page.getByRole('region', { name: '中期记忆' }).getByText('此层无匹配条目。').waitFor()
    await button(page, '文件').click()
    await page.getByRole('button', { name: /release\.md/ }).click()
    let sent = false
    page.on('request', request => { if (request.url().includes('/agent/chat/stream')) sent = true })
    await button(page, '带到对话').click()
    assert.match(await page.getByTestId('chat-input').inputValue(), /Approved release plan/)
    assert.equal(await page.getByRole('checkbox', { name: '保存本轮内容' }).isChecked(), false)
    assert.equal(sent, false)
    assert.deepEqual(errors, [])
    console.log('PASS lifecycle topic and lane views, independent retry and unsent chat draft')

    await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '项目资料' }).click()
    const outgoing = []
    page.on('request', request => { if (request.method() === 'POST') outgoing.push(request.url()) })
    const openPractice = button(page, '打开订单核验练习')
    await openPractice.press('Enter')
    const order = page.getByRole('heading', { name: '待核验订单.md' })
    await order.waitFor()
    assert.equal(await order.evaluate(element => document.activeElement === element), true)
    await page.getByText(/数量：2 件/).waitFor()
    await page.getByRole('button', { name: /客户确认单\.md/ }).click()
    await page.getByText(/数量：3 件/).waitFor()
    await button(page, '订单核验步骤').click()
    await button(page, '查看来源（v1）').click()
    await openPractice.press('Enter')
    await order.waitFor()
    assert.equal(await page.getByRole('button', { name: /订单核验练习（虚构样例）/ }).count(), 1)
    assert.deepEqual(outgoing, [])
    assert.deepEqual(errors, [])
    console.log('PASS keyboard entry, conflicting source fields, source link, no duplicate project or POST')
  } catch (error) {
    await captureFailure(page, 'project-library', error)
    throw error
  } finally {
    await browser.close()
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
