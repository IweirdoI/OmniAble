import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { requestApi } from './ingestion.js'

const stages = [
  { id: 'short', name: '短期', route: 'short-term', hint: '近期写入、等待使用的记忆' },
  { id: 'mid', name: '中期', route: 'mid-term', hint: '留待整合与复用的记忆' },
  { id: 'long', name: '长期', route: 'memories', hint: '已保存的当前有效记忆' },
]
const sampleLimit = 8
const categories = [
  ['preference', '偏好'], ['document_chunk', '知识资料'], ['tool_call', '操作经验'],
  ['project_context', '项目上下文'], ['feedback', '反馈'], ['knowledge', '知识'],
]

function preview(item, stage) {
  const metadata = item.metadata || {}
  return {
    id: item.memory_id || item.id,
    stage,
    content: item.content,
    type: item.memory_type,
    source: item.source || metadata.source_type || metadata.source,
    filename: item.filename || metadata.filename,
    scene: item.scene_id,
    version: item.version ?? metadata.version,
    createdAt: item.created_at,
    updatedAt: item.updated_at,
    lastUsedAt: item.last_used_at || metadata.last_accessed_at,
    accessCount: metadata.access_count,
    supersedes: item.supersedes_memory_id || metadata.supersedes_memory_id,
  }
}

export default {
  props: { user: String, live: Boolean },
  emits: ['forget'],
  setup(props) {
    const view = ref('topics'), query = ref(''), selected = ref(null)
    const snapshots = ref({})
    const pending = new Map()
    const filtered = items => items.filter(item => [item.content, item.filename, item.scene, item.source].some(value =>
      typeof value === 'string' && value.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase())))
    const groups = computed(() => {
      const items = stages.flatMap(stage => snapshots.value[stage.id]?.items || [])
      return [...categories, ['other', '其他']].map(([key, name]) => ({ name, items: filtered(items.filter(item => key === 'other'
        ? !categories.some(([category]) => category === item.type) : item.type === key)) })).filter(group => group.items.length)
    })
    const stageName = id => stages.find(stage => stage.id === id).name
    const typeName = value => categories.find(([category]) => category === value)?.[1] || '其他'
    const refresh = async stage => {
      if (!props.live) return
      const operation = Symbol(stage.id)
      pending.set(stage.id, operation)
      snapshots.value[stage.id] = { state: 'loading', items: [] }
      if (selected.value?.stage === stage.id) selected.value = null
      try {
        const data = await requestApi(`/${stage.route}/${encodeURIComponent(props.user)}?limit=${sampleLimit}`, {}, 12000)
        if (pending.get(stage.id) !== operation) return
        if (!Array.isArray(data.items)) throw new Error('记忆列表响应格式无效。')
        snapshots.value[stage.id] = { state: 'ready', total: data.total, items: data.items.map(item => preview(item, stage.id)) }
      } catch (error) {
        if (pending.get(stage.id) === operation) snapshots.value[stage.id] = { state: 'error', items: [], error: error.message }
      }
    }
    const reload = () => { for (const stage of stages) refresh(stage) }
    watch(() => [props.user, props.live], () => {
      pending.clear(); selected.value = null; snapshots.value = {}; reload()
    }, { immediate: true })
    onBeforeUnmount(() => pending.clear())
    return { stages, snapshots, view, query, selected, filtered, groups, stageName, typeName, refresh, reload }
  },
  template: `
    <section class="project-memory" aria-label="记忆流转">
      <header><div><h2>个人记忆流转</h2><p>当前用户 {{ user }} · 每层最多预览 8 条</p></div><button type="button" class="project-button" v-if="live" @click="reload">刷新分层</button></header>
      <p v-if="!live" class="project-detail-empty">连接 API 后查看真实短期、中期和长期记忆。</p>
      <template v-else>
        <div class="project-filters"><label class="sr-only" for="memory-preview-search">筛选当前记忆预览</label><input id="memory-preview-search" type="search" placeholder="筛选当前预览" v-model="query" /><button class="project-button" type="button" :aria-pressed="view === 'topics'" @click="view = 'topics'">主题列表</button><button class="project-button" type="button" :aria-pressed="view === 'lanes'" @click="view = 'lanes'">分层泳道</button></div>
        <div v-for="stage in stages" :key="stage.id" v-show="snapshots[stage.id]?.state === 'error'" class="project-error" role="status">{{ stage.name }}读取失败。{{ snapshots[stage.id]?.error }} <button class="project-button" type="button" @click="refresh(stage)">重试{{ stage.name }}</button></div>
        <p v-if="stages.some(stage => snapshots[stage.id]?.state === 'loading')" role="status">正在读取记忆…</p>
        <div v-if="view === 'topics'" class="project-topic-list">
          <div class="project-topic" v-for="group in groups" :key="group.name"><h3>{{ group.name }}</h3><div class="project-rows"><button type="button" v-for="item in group.items" :key="item.stage + ':' + item.id" :aria-pressed="selected === item" @click="selected = item"><b>{{ item.content }}</b><small>{{ stageName(item.stage) }} · {{ item.filename || item.source || '来源未记录' }}</small></button></div></div>
          <p v-if="!groups.length && stages.every(stage => snapshots[stage.id]?.state === 'ready')" class="project-detail-empty">当前预览没有匹配条目。</p>
        </div>
        <div v-else class="project-lanes">
          <section v-for="stage in stages" :key="stage.id" class="project-lane" :aria-label="stage.name + '记忆'">
            <h3>{{ stage.name }} <small v-if="Number.isInteger(snapshots[stage.id]?.total)">{{ snapshots[stage.id].total }} 条</small></h3><p>{{ stage.hint }}</p>
            <div class="project-rows"><button type="button" v-for="item in filtered(snapshots[stage.id]?.items || [])" :key="item.id" :aria-pressed="selected === item" @click="selected = item"><b>{{ item.content }}</b><small>{{ typeName(item.type) }} · {{ item.filename || item.source || '来源未记录' }}</small></button></div>
            <p v-if="snapshots[stage.id]?.state === 'ready' && !filtered(snapshots[stage.id].items).length">此层无匹配条目。</p>
          </section>
        </div>
        <section v-if="selected" class="project-memory-detail" aria-label="记忆详情">
          <header><h3>记忆详情</h3><button class="project-button" type="button" @click="selected = null">关闭详情</button></header>
          <pre class="project-preview">{{ selected.content }}</pre>
          <dl><dt>当前层级</dt><dd>{{ stageName(selected.stage) }}</dd><dt>来源</dt><dd>{{ selected.filename || selected.source || '未记录' }}</dd><dt>场景</dt><dd>{{ selected.scene || '全局' }}</dd><dt>版本</dt><dd>{{ selected.version ?? '未记录' }}</dd></dl>
          <h4>已记录的变化</h4>
          <ol v-if="selected.createdAt || selected.updatedAt || selected.lastUsedAt || selected.supersedes">
            <li v-if="selected.createdAt">创建于 {{ selected.createdAt }}</li><li v-if="selected.updatedAt && selected.updatedAt !== selected.createdAt">更新于 {{ selected.updatedAt }}</li><li v-if="selected.lastUsedAt">最近引用 {{ selected.lastUsedAt }}</li><li v-if="selected.supersedes">替代记忆 {{ selected.supersedes }}</li>
          </ol><p v-else>该条目没有可显示的历史记录。</p>
          <p v-if="Number.isInteger(selected.accessCount)">记录的使用次数：{{ selected.accessCount }}</p>
          <p>分层视图展示当前快照，晋升原因仅在后台有记录时才能追溯。</p>
          <button v-if="selected.id" type="button" class="project-button danger" @click="$emit('forget', selected)">准备遗忘</button>
        </section>
      </template>
    </section>
  `,
}
