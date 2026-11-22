import assert from 'node:assert/strict'
import test from 'node:test'
import {
  defaultPluginIds,
  filterPlugins,
  loadInstalledPluginIds,
  normalizeInstalledPluginIds,
  pluginCatalog,
  saveInstalledPluginIds,
} from '../src/plugin-catalog.js'

const createStorage = (initial = {}) => {
  const values = new Map(Object.entries(initial))
  return {
    getItem: key => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, value),
  }
}

test('plugin catalog only accepts known unique ids and keeps an explicit empty install set', () => {
  assert.deepEqual(normalizeInstalledPluginIds(['quick-prompts', 'quick-prompts', 'remote-unknown']), ['quick-prompts'])
  assert.deepEqual(normalizeInstalledPluginIds([]), [])
  assert.deepEqual(normalizeInstalledPluginIds('not-an-array'), [...defaultPluginIds])
})

test('plugin install state persists, reloads and falls back safely', () => {
  const storage = createStorage()
  assert.deepEqual(loadInstalledPluginIds(storage), [...defaultPluginIds])
  saveInstalledPluginIds(['chat-export', 'chat-export', 'unknown'], storage)
  assert.deepEqual(loadInstalledPluginIds(storage), ['chat-export'])
  const broken = createStorage({ 'omnidable.workbench.plugins.v1': '{broken' })
  assert.deepEqual(loadInstalledPluginIds(broken), [...defaultPluginIds])
})

test('plugin filtering combines installed state, search and capability labels', () => {
  assert.deepEqual(filterPlugins(pluginCatalog, ['quick-prompts'], '', 'installed').map(plugin => plugin.id), ['quick-prompts'])
  assert.deepEqual(filterPlugins(pluginCatalog, ['quick-prompts'], '', 'available').map(plugin => plugin.id), ['chat-export', 'conversation-stats'])
  assert.deepEqual(filterPlugins(pluginCatalog, ['quick-prompts'], '导出', 'all').map(plugin => plugin.id), ['chat-export'])
  assert.deepEqual(filterPlugins(pluginCatalog, [], '读取当前召回', 'all').map(plugin => plugin.id), ['conversation-stats'])
  assert.deepEqual(filterPlugins(pluginCatalog, ['quick-prompts'], 'not-found', 'all'), [])
})
