// TASK-027：单开关等价输入——逐项扫描导航。空格步进 + 确认键激活的
// "等价逐项导航"口径（issue #35 验收允许）。独立于 Vue，可在浏览器回归
// 中直接驱动 DOM 验证。扫描目标 = 指定容器内可见、可交互、未禁用的元素，
// 按 DOM 顺序成环。

const DEFAULT_CONTAINERS = ['.topbar', '.session-rail', '.chat-panel', '.memory-panel']
const INTERACTIVE = 'button:not([disabled]), input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'

const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)

export function createScanController({
  containerSelectors = DEFAULT_CONTAINERS,
  highlightClass = 'scan-active',
  root = () => document,
} = {}) {
  let active = false
  let index = -1
  let targets = []

  function collect() {
    const scope = root()
    const found = []
    for (const selector of containerSelectors) {
      for (const container of scope.querySelectorAll(selector)) {
        if (!visible(container)) continue
        for (const el of container.querySelectorAll(INTERACTIVE)) {
          if (el.closest('[data-scan-skip], [inert], [aria-hidden="true"]')) continue
          if (el.matches(':disabled, [aria-disabled="true"]')) continue
          if (visible(el) && !found.includes(el)) found.push(el)
        }
      }
    }
    return found
  }

  function paint(next, focus = false) {
    for (const el of targets) el.classList.remove(highlightClass)
    targets = next
    if (index >= 0 && targets[index]) {
      const el = targets[index]
      el.classList.add(highlightClass)
      if (focus) el.focus({ preventScroll: true })
      el.scrollIntoView({ block: 'nearest' })
    }
  }

  function start() {
    if (active) return
    active = true
    index = -1
    paint(collect())
  }

  function stop() {
    if (!active) return
    active = false
    index = -1
    paint([])
  }

  function refresh() {
    if (!active) return
    index = -1
    paint(collect())
  }

  // 步进：越过失效目标；整轮无目标则保持空转。返回当前高亮元素或 null。
  function step() {
    if (!active) return null
    const previous = current()
    const next = collect()
    if (!next.length) {
      index = -1
      paint([])
      return null
    }
    index = (next.indexOf(previous) + 1) % next.length
    paint(next, true)
    return next[index]
  }

  function current() {
    return active && targets[index] && targets[index].isConnected ? targets[index] : null
  }

  // 激活：聚焦后触发 click（input 聚焦即可用，click 对按钮/链接生效）。
  function activate() {
    const el = current()
    if (!el || !collect().includes(el)) return null
    el.focus({ preventScroll: true })
    if (typeof el.click === 'function') el.click()
    return el
  }

  return { start, stop, refresh, step, activate, current, collect }
}
