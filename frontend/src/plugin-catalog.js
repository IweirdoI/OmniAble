import QuickPromptsPanel from './plugin-panels/QuickPromptsPanel.js'
import ChatExportPanel from './plugin-panels/ChatExportPanel.js'
import ConversationStatsPanel from './plugin-panels/ConversationStatsPanel.js'

export const pluginCapabilityLabels = {
  'composer:insert': '写入输入框',
  'conversation:read': '读取当前对话',
  'memory:read': '读取当前召回',
}

export const pluginCatalog = Object.freeze([
  {
    id: 'quick-prompts',
    name: '快捷指令',
    glyph: '✦',
    tone: 'blue',
    version: '0.1.0',
    author: 'OmniAble',
    description: '把常用提问整理成模板，选择后插入输入框，不会自动发送。',
    capabilities: ['composer:insert'],
    component: QuickPromptsPanel,
  },
  {
    id: 'chat-export',
    name: '对话导出',
    glyph: '↓',
    tone: 'mint',
    version: '0.1.0',
    author: 'OmniAble',
    description: '把当前临时会话导出为 Markdown 文件，方便归档或继续编辑。',
    capabilities: ['conversation:read'],
    component: ChatExportPanel,
  },
  {
    id: 'conversation-stats',
    name: '会话概览',
    glyph: '◫',
    tone: 'amber',
    version: '0.1.0',
    author: 'OmniAble',
    description: '在面板内查看当前对话轮次、文本量和本轮召回数量。',
    capabilities: ['conversation:read', 'memory:read'],
    component: ConversationStatsPanel,
  },
])

const STORAGE_KEY = 'omnidable.workbench.plugins.v1'
export const defaultPluginIds = Object.freeze(['quick-prompts'])

export function normalizeInstalledPluginIds(value, catalog = pluginCatalog) {
  if (!Array.isArray(value)) return [...defaultPluginIds]
  const known = new Set(catalog.map(plugin => plugin.id))
  return [...new Set(value.filter(id => typeof id === 'string' && known.has(id)))]
}

export function loadInstalledPluginIds(storage = globalThis.localStorage) {
  try {
    const raw = storage?.getItem(STORAGE_KEY)
    if (raw === null || raw === undefined) return [...defaultPluginIds]
    return normalizeInstalledPluginIds(JSON.parse(raw))
  } catch {
    return [...defaultPluginIds]
  }
}

export function saveInstalledPluginIds(ids, storage = globalThis.localStorage) {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify(normalizeInstalledPluginIds(ids)))
  } catch {
    // Private browsing or a storage quota issue should not break the workbench.
  }
}

export function filterPlugins(catalog, installedIds, query, filter) {
  const installed = new Set(installedIds)
  const needle = String(query || '').trim().toLowerCase()
  return catalog.filter(plugin => {
    if (filter === 'installed' && !installed.has(plugin.id)) return false
    if (filter === 'available' && installed.has(plugin.id)) return false
    if (!needle) return true
    const haystack = [plugin.name, plugin.description, ...plugin.capabilities.map(key => pluginCapabilityLabels[key] || key)]
      .join(' ')
      .toLowerCase()
    return haystack.includes(needle)
  })
}
