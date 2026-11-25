const fs = require('node:fs')
const path = require('node:path')

function artifactDir() {
  const dir = process.env.BROWSER_ARTIFACT_DIR || path.resolve(__dirname, '..', 'test-results')
  fs.mkdirSync(dir, { recursive: true })
  return dir
}

function launchOptions() {
  const channel = process.env.PLAYWRIGHT_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined)
  return channel ? { channel, headless: true } : { headless: true }
}

async function captureFailure(page, name, error) {
  const dir = artifactDir()
  if (page) {
    try {
      await page.screenshot({ path: path.join(dir, `${name}-failure.png`), fullPage: true })
    } catch {}
  }
  fs.writeFileSync(path.join(dir, `${name}-failure.log`), `${error?.stack || error}\n`, 'utf8')
}

module.exports = { artifactDir, captureFailure, launchOptions }