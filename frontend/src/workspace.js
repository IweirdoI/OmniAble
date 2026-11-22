import { requestApi } from './ingestion.js'

export function normalizeWebUrl(value) {
  const raw = String(value || '').trim()
  if (!raw) throw new Error('请输入网页地址')
  const url = new URL(raw.includes('://') ? raw : `https://${raw}`)
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || /[\u0000-\u0020]/.test(raw)) {
    throw new Error('仅支持不带账号密码的 HTTP(S) 网页地址')
  }
  return url.href
}

export async function workspaceRequest(path, options = {}) {
  try { return await requestApi(`/workspace${path}`, options, 15000) }
  catch (error) {
    if (error.message.startsWith('请求超过')) throw new Error('工作区请求超时；操作可能已完成，请刷新目录和变更记录确认。')
    throw error
  }
}

export function fileLink(href) {
  if (!href || /^(?:[a-z][a-z\d+.-]*:|\/\/|#)/i.test(href)) return null
  const clean = decodeURIComponent(href.split('#')[0].split('?')[0]).replace(/^\.\//, '')
  if (!clean || clean.startsWith('/') || clean.split(/[\\/]/).includes('..')) return null
  return clean
}

export function diffKind(line) {
  if (line.startsWith('+++') || line.startsWith('---')) return 'header'
  if (line.startsWith('@@')) return 'hunk'
  return line.startsWith('+') ? 'addition' : line.startsWith('-') ? 'deletion' : 'context'
}
