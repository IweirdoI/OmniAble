import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { diffKind, normalizeWebUrl, workspaceRequest as api } from './workspace.js'
import './workspace.css'

export default {
  props: { target: Object, refreshKey: Number, visible: Boolean },
  setup(props) {
    const mode = ref('files'), enabled = ref(false), root = ref('工作区')
    const busy = ref(false), error = ref(''), notice = ref('')
    const folder = ref('.'), entries = ref([]), next = ref(null), filter = ref('')
    const path = ref(''), content = ref(''), original = ref(''), version = ref(null)
    const loaded = ref(false), creating = ref(false), newPath = ref('')
    const changes = ref([]), selectedChange = ref(null), diff = ref('')
    const webDraft = ref(''), pages = ref([]), activePage = ref(''), webLoading = ref(false)
    const dirty = computed(() => (loaded.value || creating.value) && (content.value !== original.value || creating.value && newPath.value.trim() !== ''))
    const stale = computed(() => loaded.value && changes.value.some(c => c.path === path.value && c.revision !== version.value))
    const filtered = computed(() => entries.value.filter(e => e.name.toLowerCase().includes(filter.value.toLowerCase())))
    const page = computed(() => pages.value.find(p => p.url === activePage.value))
    const lines = computed(() => diff.value.split('\n'))
    const breadcrumbs = computed(() => {
      const bits = folder.value === '.' ? [] : folder.value.split('/')
      return [{ label: root.value, path: '.' }, ...bits.map((label, i) => ({ label, path: bits.slice(0, i+1).join('/') }))]
    })
    const guard = () => !dirty.value || window.confirm('当前文件有未保存的编辑，放弃这些编辑？')
    const run = async fn => {
      if (busy.value) return
      busy.value = true; error.value = ''; notice.value = ''
      try { await fn() } catch (e) { error.value = e.message } finally { busy.value = false }
    }
    const list = async (dest = folder.value, more = false) => {
      const data = await api(`/tree?path=${encodeURIComponent(dest)}&offset=${more ? next.value : 0}`)
      folder.value = data.path; entries.value = more ? [...entries.value, ...data.entries] : data.entries
      next.value = data.next_offset
    }
    const refreshChanges = async () => {
      changes.value = (await api('/changes')).changes
      if (selectedChange.value) {
        if (changes.value.some(c => c.path === selectedChange.value.path)) {
          const data = await api(`/diff?path=${encodeURIComponent(selectedChange.value.path)}`)
          selectedChange.value = data; diff.value = data.diff
        } else { selectedChange.value = null; diff.value = '' }
      }
    }
    const refresh = () => run(async () => {
      const info = await api(''); enabled.value = info.enabled; root.value = info.name || '工作区'
      if (info.enabled) { await list(); await refreshChanges() }
    })
    const read = async dest => {
      const data = await api(`/file?path=${encodeURIComponent(dest)}`)
      path.value = data.path; content.value = data.content; original.value = data.content
      version.value = data.revision; loaded.value = true; creating.value = false
    }
    const openFile = dest => {
      if (!guard()) return
      return run(async () => { await read(dest); mode.value = 'files' })
    }
    const openEntry = entry => entry.kind === 'directory' ? run(() => list(entry.path)) : openFile(entry.path)
    const create = () => {
      if (!guard()) return
      creating.value = true; newPath.value = folder.value === '.' ? '' : folder.value + '/'
      loaded.value = false; content.value = ''; original.value = ''; path.value = ''; version.value = null
    }
    const save = () => run(async () => {
      const dest = creating.value ? newPath.value.trim() : path.value
      if (!dest) throw new Error('请填写新文件相对路径')
      await api('/file', { method: 'PUT', body: JSON.stringify({ path: dest, content: content.value, revision: version.value }) })
      await read(dest); await list(); await refreshChanges(); notice.value = '已保存到工作区'
    })
    const remove = () => run(async () => {
      await api('/file', { method: 'DELETE', body: JSON.stringify({ path: path.value, revision: version.value }) })
      loaded.value = false; content.value = ''; original.value = ''
      await list(); await refreshChanges(); mode.value = 'changes'; notice.value = '文件已删除，可在变更中恢复'
    })
    const showDiff = dest => run(async () => {
      const data = await api(`/diff?path=${encodeURIComponent(dest)}`)
      selectedChange.value = data; diff.value = data.diff; mode.value = 'changes'
    })
    const resolveChange = restore => run(async () => {
      const item = selectedChange.value
      if (!item || !guard()) return
      if (!restore && !window.confirm('接受当前磁盘内容并清除此文件的恢复记录？此操作不会提交 Git。')) return
      const result = await api(restore ? '/restore' : '/accept', { method: 'POST', body: JSON.stringify({ path: item.path, revision: item.revision }) })
      if (loaded.value && path.value === item.path) { loaded.value = false; content.value = ''; original.value = '' }
      selectedChange.value = null; diff.value = ''; await list(); await refreshChanges()
      notice.value = restore ? result.recovered ? '已恢复删除前的文件内容' : '已恢复到首次修改前' : '已接受变更并清除恢复记录'
    })
    const openWeb = value => {
      try {
        error.value = ''
        const url = normalizeWebUrl(value)
        if (!pages.value.some(p => p.url === url)) {
          if (pages.value.length >= 8) pages.value.shift()
          pages.value.push({ url, title: new URL(url).hostname })
        }
        activePage.value = url; webDraft.value = url; mode.value = 'web'; webLoading.value = true
      } catch (e) { error.value = e.message }
    }
    const closePage = url => {
      pages.value = pages.value.filter(p => p.url !== url)
      if (activePage.value === url) activePage.value = pages.value.at(-1)?.url || ''
    }
    const beforeUnload = event => { if (dirty.value) { event.preventDefault(); event.returnValue = '' } }
    onMounted(() => { refresh(); window.addEventListener('beforeunload', beforeUnload) })
    onBeforeUnmount(() => { window.removeEventListener('beforeunload', beforeUnload) })
    watch(() => props.target, async target => {
      if (!target) return
      if (target.kind === 'web') openWeb(target.path)
      else if (target.kind === 'changes') { mode.value = 'changes'; await refresh(); if (target.path) await showDiff(target.path) }
      else await openFile(target.path)
    })
    watch(() => props.refreshKey, () => { if (enabled.value) run(async () => { await list(); await refreshChanges() }) })
    watch(() => props.visible, value => { if (value) refresh() })
    return { mode, enabled, root, busy, error, notice, folder, entries, next, filter, filtered, breadcrumbs,
      path, content, loaded, creating, newPath, dirty, stale, changes, selectedChange, diff, lines, diffKind,
      webDraft, pages, activePage, page, webLoading, refresh, list, run, openEntry, openFile, create, save, remove,
      showDiff, resolveChange, openWeb, closePage }
  },
  template: `
    <section class="workspace" aria-label="文件与网页工作区">
      <header class="ws-header"><div><span class="eyebrow">AGENT WORKSPACE</span><h2>{{ root }}</h2></div><button @click="refresh" :disabled="busy" aria-label="刷新工作区">↻</button></header>
      <nav class="ws-tabs" aria-label="工作区导航">
        <button :class="{active: mode === 'files'}" @click="mode = 'files'">▤ 文件</button>
        <button :class="{active: mode === 'changes'}" @click="mode = 'changes'; refresh()">± 变更 <span v-if="changes.length">{{ changes.length }}</span></button>
        <button :class="{active: mode === 'web'}" @click="mode = 'web'">◎ 网页</button>
      </nav>
      <p v-if="error" class="ws-error" role="alert">{{ error }}</p><p v-if="notice" class="ws-notice" role="status">{{ notice }}</p>
      <div v-if="!enabled && mode !== 'web'" class="ws-empty"><b>连接你的工作目录</b><p>在服务端设置 AGENT_ENABLE_WORKSPACE=1，并用 AGENT_EXEC_CWD 指定工作目录，重启后刷新。</p><small>文件操作仅在指定目录内执行，记忆资料仍在“记忆”面板。</small></div>
      <div v-else-if="mode === 'files'" class="ws-files">
        <div class="ws-tree">
          <div class="ws-breadcrumbs"><button v-for="b in breadcrumbs" :key="b.path" @click="run(() => list(b.path))" :disabled="busy">{{ b.label }} /</button></div>
          <div class="ws-tree-actions"><input v-model="filter" placeholder="筛选当前目录…" aria-label="筛选文件" /><button @click="create" :disabled="busy" title="新建文本文件" aria-label="新建文件">＋</button></div>
          <div class="ws-entry-list"><button v-for="entry in filtered" :key="entry.path" class="ws-entry" :class="{selected: path === entry.path && loaded}" :disabled="busy" @click="openEntry(entry)" :title="entry.path"><span :class="entry.kind">{{ entry.kind === 'directory' ? '▸' : '⌑' }}</span>{{ entry.name }}</button><p v-if="!filtered.length" class="ws-muted">此目录没有可显示的文件</p><button v-if="next !== null" @click="run(() => list(folder, true))" :disabled="busy">加载更多</button></div>
        </div>
        <div class="ws-editor">
          <template v-if="loaded || creating">
            <div class="ws-filebar"><input v-if="creating" v-model="newPath" :readonly="busy" placeholder="例如 notes/plan.md" aria-label="新文件路径" /><b v-else :title="path">{{ path }}</b><span v-if="dirty || creating" class="ws-unsaved">未保存</span></div>
            <div class="ws-editor-tools"><span>UTF-8 · {{ content.split('\\n').length }} 行</span><button @click="save" :disabled="busy || (!dirty && !creating)">保存</button><button v-if="loaded" @click="openFile(path)" :disabled="busy">重新读取</button><button v-if="loaded" @click="remove" :disabled="busy || dirty || stale" class="ws-delete">删除</button></div>
            <p v-if="stale" class="ws-error">磁盘文件已有变化；这里保留的是打开时的内容。<button @click="showDiff(path)" :disabled="busy">查看变更</button></p>
            <textarea v-model="content" :readonly="busy" class="ws-code-input" aria-label="文件内容" spellcheck="false" @keydown.ctrl.s.prevent="save" @keydown.meta.s.prevent="save"></textarea>
            <footer class="ws-file-footer">{{ busy ? '正在处理…' : 'Ctrl / ⌘ S 保存 · 删除后可从变更恢复' }}</footer>
          </template>
          <div v-else class="ws-empty"><span class="ws-empty-icon">⌑</span><b>在这里查看和编辑</b><p>从左侧选择文件，或新建一份文本。<br />对话中的文件也可以直接打开。</p></div>
        </div>
      </div>
      <div v-else-if="mode === 'changes'" class="ws-changes">
        <div class="ws-change-list"><p class="ws-muted">工作区操作记录 · 非 Git 暂存区</p><button v-for="item in changes" :key="item.path" :disabled="busy" :class="{selected: selectedChange?.path === item.path}" @click="showDiff(item.path)"><span>{{ item.status === 'deleted' ? 'D' : item.status === 'added' ? 'A' : 'M' }}</span><b :title="item.path">{{ item.path }}</b><small v-if="item.conflict">外部变化</small><em>+{{ item.added || 0 }} −{{ item.removed || 0 }}</em></button><div v-if="!changes.length" class="ws-empty"><b>还没有文件变更</b><p>你和 Agent 的文件修改会显示在这里。</p></div></div>
        <template v-if="selectedChange"><div class="ws-diffbar"><b>{{ selectedChange.path }}</b><button v-if="!selectedChange.deleted" @click="openFile(selectedChange.path)" :disabled="busy">打开文件</button><button @click="resolveChange(true)" :disabled="busy || selectedChange.conflict">恢复</button><button @click="resolveChange(false)" :disabled="busy">接受</button></div><p v-if="selectedChange.conflict" class="ws-error">文件在记录后被外部修改。恢复已锁定，请打开文件比较。</p><pre class="ws-diff" aria-label="文件差异"><code v-for="(line, i) in lines" :key="i" :class="diffKind(line)">{{ line || ' ' }}\n</code></pre></template>
      </div>
      <div v-else-if="mode === 'web'" class="ws-browser">
        <form class="ws-address" @submit.prevent="openWeb(webDraft)"><label class="sr-only" for="ws-url">网页地址</label><input id="ws-url" v-model="webDraft" placeholder="输入 https://…" type="text" /><button>打开 ↗</button></form>
        <div v-if="pages.length" class="ws-page-tabs"><div v-for="p in pages" :key="p.url" :class="{active: activePage === p.url}"><button @click="activePage = p.url; webDraft = p.url">{{ p.title }}</button><button @click="closePage(p.url)" :aria-label="'关闭 ' + p.title">×</button></div></div>
        <template v-if="page"><div class="ws-browser-note"><span>部分站点禁止嵌入；空白时请在浏览器打开。</span><a :href="page.url" target="_blank" rel="noopener noreferrer">外部打开 ↗</a></div><iframe :key="page.url" :src="page.url" :title="'网页预览：' + page.title" sandbox="allow-scripts allow-forms allow-popups" referrerpolicy="no-referrer" @load="webLoading = false"></iframe></template>
        <div v-else class="ws-empty"><span class="ws-empty-icon">◎</span><b>把网页放在对话旁边</b><p>输入网页地址，或点击回答中的网页链接。<br />最多保留 8 个页面标签。</p></div>
      </div>
    </section>
  `,
}
