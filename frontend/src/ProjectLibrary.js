import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  BUNDLE_LIMIT, addFile, addProject, addVersion, exportProject, importProject,
  loadLibrary, moveFile, noteStatus, projectFileAccept, projectStorageKey, readProjectFile, saveLibrary, saveNote,
} from './project-library.js'
import MemoryLifecycle from './MemoryLifecycle.js'
import OrderVerification from './order-verification.js'
import SubtitleCorrection from './subtitle-correction.js'
import OutboundExpression from './outbound-expression.js'
import { createOrderPractice, orderDraftName, orderPracticeId, orderSourceName } from './order-practice.js'
import './project-library.css'

export default {
  components: { MemoryLifecycle, OrderVerification, SubtitleCorrection, OutboundExpression },
  props: { user: String, live: Boolean, visible: Boolean, busyChat: Boolean },
  emits: ['chat', 'workspace', 'ingest', 'memory', 'ask', 'forget'],
  setup(props, { emit }) {
    const projects = ref([]), activeId = ref(''), view = ref('files'), folder = ref(''), query = ref('')
    const selectedId = ref(''), chosenVersion = ref(null), form = ref(null)
    const error = ref(''), status = ref(''), busy = ref(false)
    let loadedSnapshot
    const project = computed(() => projects.value.find(item => item.id === activeId.value))
    const folders = computed(() => [...new Set(project.value?.files.map(file => file.folder) || [])].sort())
    const matches = (...fields) => fields.some(field => field.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase()))
    const files = computed(() => project.value?.files.filter(file => (!folder.value || file.folder === folder.value) && matches(file.name, file.folder)) || [])
    const notes = computed(() => project.value?.notes.filter(note => matches(note.title, note.body)) || [])
    const source = computed(() => project.value?.files.find(file => file.id === selectedId.value))
    const orderSource = computed(() => project.value?.files.find(file => file.name === orderSourceName))
    const orderDraft = computed(() => project.value?.files.find(file => file.name === orderDraftName))
    const showOrderVerification = computed(() => activeId.value === orderPracticeId && source.value?.id === orderDraft.value?.id && Boolean(orderSource.value))
    const showSubtitleCorrection = computed(() => activeId.value === orderPracticeId && source.value?.name === orderDraftName)
    const showOutboundExpression = computed(() => activeId.value === orderPracticeId && source.value?.name === orderDraftName)
    const note = computed(() => project.value?.notes.find(item => item.id === selectedId.value))
    const noteSource = computed(() => project.value?.files.find(file => file.id === note.value?.sourceId))
    const hasNoteVersion = computed(() => noteSource.value?.versions.some(item => item.number === note.value.sourceVersion))
    const version = computed(() => source.value?.versions.find(item => item.number === chosenVersion.value) || source.value?.versions.at(-1))
    const sharePreview = computed(() => {
      if (form.value?.kind !== 'share' || !form.value.fileIds.length || !project.value) return null
      try { return exportProject(project.value, form.value.fileIds) } catch { return null }
    })
    const visibleNotesForSource = computed(() => project.value?.notes.filter(item => item.sourceId === source.value?.id) || [])
    const clearDetail = () => { selectedId.value = ''; chosenVersion.value = null; form.value = null }
    const selectProject = id => { activeId.value = id; folder.value = ''; query.value = ''; view.value = 'files'; clearDetail() }
    const switchView = next => { view.value = next; query.value = ''; clearDetail() }
    const reload = () => {
      try {
        projects.value = loadLibrary(localStorage, props.user)
        loadedSnapshot = localStorage.getItem(projectStorageKey(props.user))
        if (!project.value) selectProject(projects.value[0]?.id || '')
      } catch (failure) { error.value = failure.message }
    }
    const storageChanged = event => {
      if (event.key !== null && event.key !== projectStorageKey(props.user)) return
      const previousSelection = selectedId.value
      const previousView = view.value
      reload()
      if (previousView === 'files' && project.value?.files.some(file => file.id === previousSelection)) {
        selectedId.value = previousSelection
        chosenVersion.value = null
        form.value = null
      } else {
        clearDetail()
      }
      status.value = '已同步此浏览器其他页面的项目变更。'
    }
    onMounted(() => { reload(); window.addEventListener('storage', storageChanged) })
    onBeforeUnmount(() => window.removeEventListener('storage', storageChanged))
    const run = async operation => {
      if (busy.value) return
      error.value = ''; status.value = ''; busy.value = true
      try { await operation() } catch (failure) { error.value = failure.message } finally { busy.value = false }
    }
    const commit = (items, message) => {
      const key = projectStorageKey(props.user)
      if (loadedSnapshot === undefined) throw new Error('当前项目数据无法读取。请先备份浏览器中的项目数据。')
      if (loadedSnapshot !== localStorage.getItem(key)) {
        clearDetail()
        reload()
        throw new Error('已有其他页面保存了新内容，请重新操作。')
      }
      try { saveLibrary(localStorage, props.user, items) }
      catch { throw new Error('无法保存到浏览器。请检查剩余空间，或导出后删除不再需要的资料。') }
      loadedSnapshot = localStorage.getItem(key)
      projects.value = items
      status.value = message
    }
    const update = (current, changes, message) => {
      if (project.value !== current) throw new Error('项目在操作期间发生变化，请重新打开条目后重试。')
      commit(projects.value.map(item => item.id === current.id ? { ...item, ...changes } : item), message)
    }
    const openForm = async data => {
      error.value = ''; form.value = data
      await nextTick()
      document.getElementById('project-form-heading')?.focus({ preventScroll: false })
    }
    const makeProject = () => run(() => {
      const item = addProject(projects.value, form.value.name)
      commit([...projects.value, item], '项目已创建。')
      selectProject(item.id)
    })
    const openOrderPractice = () => run(async () => {
      const existing = projects.value.find(item => item.id === orderPracticeId)
      const item = existing || createOrderPractice(projects.value)
      if (!existing) commit([...projects.value, item], '已载入虚构练习资料。')
      selectProject(item.id)
      const draftFile = item.files.find(file => file.name === '待核验订单.md')
      if (draftFile) selectFile(draftFile.id)
      await nextTick()
      document.getElementById(draftFile ? 'project-file-heading' : 'project-home-title')?.focus()
    })
    const upload = () => run(async () => {
      const current = project.value, input = form.value
      if (!input.file) throw new Error('请选择一份文本文件。')
      const content = await readProjectFile(input.file)
      if (input.kind === 'version') {
        const item = current.files.find(file => file.id === input.sourceId)
        const updated = addVersion(item, content)
        update(current, { files: current.files.map(file => file.id === item.id ? updated : file) }, '新版本已保存。关联知识卡可能需要复核。')
        selectFile(updated.id)
      } else {
        const item = addFile(current, { name: input.file.name, folder: input.folder, content })
        update(current, { files: [...current.files, item] }, '文件已存入本机项目资料。')
        folder.value = item.folder
        query.value = ''
        selectFile(item.id)
      }
    })
    const selectFile = (id, number = null) => {
      const nextFile = project.value?.files.find(file => file.id === id)
      if (nextFile && folder.value && folder.value !== nextFile.folder) folder.value = ''
      view.value = 'files'; selectedId.value = id; chosenVersion.value = number; form.value = null
    }
    const selectNote = id => { view.value = 'wiki'; selectedId.value = id; form.value = null }
    const createNote = () => openForm({ kind: 'note', title: '', body: '', sourceId: source.value.id, sourceVersion: version.value.number })
    const editNote = () => openForm({ kind: 'note', ...note.value })
    const currentNoteSource = computed(() => project.value?.files.find(file => file.id === form.value?.sourceId))
    const adoptLatest = () => { form.value.sourceVersion = currentNoteSource.value.versions.at(-1).number }
    const persistNote = () => run(() => {
      const current = project.value
      const saved = saveNote(current, form.value)
      const nextNotes = current.notes.some(item => item.id === saved.id)
        ? current.notes.map(item => item.id === saved.id ? saved : item)
        : [...current.notes, saved]
      update(current, { notes: nextNotes }, '知识卡已保存，可以沿来源核对。')
      selectNote(saved.id)
    })
    const deleteFile = () => run(() => {
      const current = project.value, item = source.value
      if (!window.confirm(`删除「${item.name}」及本机保留的版本？相关知识卡将保留并标为来源已移除。`)) return
      update(current, { files: current.files.filter(file => file.id !== item.id) }, '已移除本机文件及版本。')
      clearDetail()
    })
    const changeFolder = () => run(() => {
      const current = project.value
      const item = moveFile(current, source.value, form.value.folder)
      update(current, { files: current.files.map(file => file.id === item.id ? item : file) }, '已移动文件。')
      folder.value = item.folder
      query.value = ''
      selectFile(item.id)
    })
    const deleteNote = () => run(() => {
      if (!window.confirm(`删除知识卡「${note.value.title}」？`)) return
      update(project.value, { notes: project.value.notes.filter(item => item.id !== note.value.id) }, '已删除知识卡。')
      clearDetail()
    })
    const deleteProject = () => run(() => {
      if (!window.confirm(`删除本机项目「${project.value.name}」及其文件、知识？已交付的分享包仍可独立使用。`)) return
      const remaining = projects.value.filter(item => item.id !== activeId.value)
      commit(remaining, '已删除本机项目。')
      selectProject(remaining[0]?.id || '')
    })
    const exportSelected = () => run(() => {
      const { data } = exportProject(project.value, form.value.fileIds)
      const url = URL.createObjectURL(new Blob([data], { type: 'application/json' }))
      const link = document.createElement('a')
      link.href = url; link.download = `${project.value.name.replace(/[<>:"/\\|?*]/gu, '_')}.omniable.json`
      link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      form.value = null; status.value = '分享包已生成，可交给项目成员导入。'
    })
    const readBundle = () => run(async () => {
      const input = form.value
      if (!input.file || input.file.size > BUNDLE_LIMIT) throw new Error('请选择不超过 512 KiB 的项目分享包。')
      const raw = await input.file.text()
      if (form.value !== input) throw new Error('导入已取消，请重新选择文件。')
      input.preview = importProject(projects.value, raw)
    })
    const confirmImport = () => run(() => {
      const item = form.value.preview
      if (!item) return
      addProject(projects.value, item.name, item.id)
      commit([...projects.value, item], '已导入为独立项目。')
      selectProject(item.id)
    })
    const ask = (title, body) => emit('ask', { title, body })
    const dateLabel = value => new Date(value).toLocaleString('zh-CN', { dateStyle: 'short', timeStyle: 'short' })
    return { projects, activeId, view, folder, query, project, folders, files, notes, source, note, version, chosenVersion,
      orderSource, orderDraft, showOrderVerification, showSubtitleCorrection, showOutboundExpression,
      form, error, status, busy, sharePreview, visibleNotesForSource, currentNoteSource, noteSource, hasNoteVersion,
      views: [['files', '文件'], ['wiki', '知识页'], ['memory', '记忆流转']], projectFileAccept, selectProject, switchView, selectFile, selectNote,
      openForm, makeProject, openOrderPractice, upload, createNote, editNote, adoptLatest, persistNote, deleteFile, changeFolder, deleteNote, deleteProject,
      exportSelected, readBundle, confirmImport, ask, noteStatus, dateLabel }
  },
  template: `
    <main class="project-home" aria-labelledby="project-home-title" data-testid="project-home">
      <aside class="project-nav" aria-label="项目导航">
        <div class="project-nav-head"><h2>我的项目</h2><button type="button" class="project-button" @click="openForm({kind:'new', name:''})" :disabled="busy">＋ 新建</button></div>
        <div class="project-select-list">
          <button type="button" v-for="item in projects" :key="item.id" :aria-pressed="item.id === activeId" :disabled="busy" @click="selectProject(item.id)">{{ item.name }} <small>{{ item.files.length }}</small></button>
          <button type="button" class="project-import-link" @click="openForm({kind:'import', file:null, preview:null})" :disabled="busy">导入项目分享包</button>
          <button type="button" class="project-import-link" @click="openOrderPractice" :disabled="busy">打开订单核验练习</button>
        </div>
        <div class="project-shortcuts">
          <button type="button" @click="$emit('chat')">去对话 →</button>
          <button type="button" @click="$emit('workspace')">打开本机工作区</button>
          <button type="button" @click="$emit('memory')">偏好与遗忘</button>
        </div>
      </aside>
      <div class="project-main">
        <header class="project-header">
          <div><h1 id="project-home-title" tabindex="-1">项目资料</h1><p>{{ project ? project.name : '把原始资料整理成可以回查来源的项目知识。' }}</p></div>
          <div class="project-actions" v-if="project">
            <button type="button" class="project-button" @click="openForm({kind:'share', fileIds:[]})" :disabled="busy || !project.files.length">共享文件</button>
            <button type="button" class="project-button primary" @click="switchView('files'); openForm({kind:'upload', folder:folder || '未分类', file:null})" :disabled="busy">＋ 添加文件</button>
          </div>
        </header>
        <p v-if="error" class="project-error" role="alert">{{ error }}</p><p v-if="status" class="project-status" role="status">{{ status }}</p>
        <section v-if="form" class="project-form" aria-labelledby="project-form-heading">
          <header><h2 id="project-form-heading" tabindex="-1">{{ {new:'新建项目',upload:'添加文件',version:'新增文件版本',move:'移动到目录',note:'编辑知识卡',share:'生成分享包',import:'导入项目'}[form.kind] }}</h2><button type="button" class="project-button" @click="form = null" :disabled="busy">取消</button></header>
          <form v-if="form.kind === 'new'" @submit.prevent="makeProject">
            <label>项目名<input v-model="form.name" maxlength="80" required placeholder="例如：发布与验收" /></label><button class="project-button primary" :disabled="busy">创建项目</button>
          </form>
          <form v-else-if="['upload','version'].includes(form.kind)" @submit.prevent="upload">
            <p v-if="form.kind === 'version'">文件名和目录保持原样。每份文件保留最近 8 个版本。</p>
            <label v-if="form.kind === 'upload'">归入目录<input v-model.trim="form.folder" maxlength="80" list="project-folders" /><datalist id="project-folders"><option v-for="name in folders" :key="name" :value="name"></option></datalist></label>
            <label>UTF-8 文本文件（最多 64 KiB）<input type="file" :accept="projectFileAccept" required @change="form.file = $event.target.files[0] || null" /></label>
            <button class="project-button primary" :disabled="busy">保存文件</button>
          </form>
          <form v-else-if="form.kind === 'note'" @submit.prevent="persistNote">
            <p v-if="currentNoteSource">来源：{{ currentNoteSource.name }} · v{{ form.sourceVersion }} <button v-if="form.sourceVersion !== currentNoteSource.versions.at(-1).number" type="button" class="project-button" @click="adoptLatest">改用最新版</button></p>
            <label>知识标题<input v-model="form.title" maxlength="80" required /></label>
            <label>知识正文<textarea v-model="form.body" rows="4" maxlength="8192" required placeholder="根据来源记录可复用的事实或约定"></textarea></label>
            <button class="project-button primary" :disabled="busy || !currentNoteSource">保存知识卡</button>
          </form>
          <form v-else-if="form.kind === 'move'" @submit.prevent="changeFolder">
            <label>目标目录<input v-model="form.folder" list="project-move-folders" maxlength="80" required /><datalist id="project-move-folders"><option v-for="name in folders" :key="name" :value="name"></option></datalist></label>
            <button class="project-button primary" :disabled="busy">移动文件</button>
          </form>
          <form v-else-if="form.kind === 'share'" @submit.prevent="exportSelected">
            <p>勾选后导出当前文件版本及与之匹配的知识卡。收件人可独立保存副本。</p>
            <fieldset class="project-share-files"><legend>要共享的文件</legend><label v-for="file in project.files" :key="file.id"><input type="checkbox" v-model="form.fileIds" :value="file.id" /> {{ file.folder }} / {{ file.name }}</label></fieldset>
            <p v-if="sharePreview">将导出 {{ sharePreview.fileCount }} 份文件、{{ sharePreview.noteCount }} 张知识卡。</p>
            <p v-else-if="form.fileIds.length">超过分享包大小限制，请减少文件数量。</p>
            <button class="project-button primary" :disabled="busy || !sharePreview">下载分享包</button>
          </form>
          <form v-else @submit.prevent="form.preview ? confirmImport() : readBundle()">
            <label>OmniAble 分享包<input type="file" accept=".json" required @change="form.file = $event.target.files[0] || null; form.preview = null" /></label>
            <p v-if="form.preview">将新建「{{ form.preview.name }}」，包含 {{ form.preview.files.length }} 份文件、{{ form.preview.notes.length }} 张知识卡。</p>
            <button class="project-button primary" :disabled="busy || !form.file">{{ form.preview ? '确认导入' : '预览分享包' }}</button>
          </form>
        </section>
        <nav class="project-tabs" aria-label="资料和记忆视图">
          <button type="button" v-for="[key, title] in views" :key="key" :aria-pressed="view === key" :disabled="busy" @click="switchView(key)">{{ title }}</button>
        </nav>
        <MemoryLifecycle v-if="view === 'memory' && visible" :user="user" :live="live" @forget="$emit('forget', $event)" />
        <div v-else-if="view !== 'memory' && !project" class="project-empty">
          <h2>从一份项目资料开始</h2><p>创建项目后添加文件、整理知识卡，或导入队友发来的分享包。</p>
          <button type="button" class="project-button primary" @click="openForm({kind:'new', name:''})" :disabled="busy">创建第一个项目</button>
          <button type="button" class="project-button" @click="openForm({kind:'import', file:null, preview:null})" :disabled="busy">导入分享包</button>
        </div>
        <template v-else-if="view !== 'memory'">
          <div class="project-browser">
            <section class="project-index" :aria-label="view === 'files' ? '项目文件' : '项目知识卡'">
              <div class="project-filters">
                <label class="sr-only" for="project-search">搜索当前列表</label><input id="project-search" type="search" v-model="query" :placeholder="view === 'files' ? '搜索文件名或目录' : '搜索知识标题或正文'" />
                <template v-if="view === 'files'"><label class="sr-only" for="project-folder">按目录筛选</label><select id="project-folder" v-model="folder"><option value="">所有目录</option><option v-for="name in folders" :key="name">{{ name }}</option></select></template>
              </div>
              <div class="project-rows" v-if="view === 'files'">
                <button type="button" v-for="item in files" :key="item.id" :aria-pressed="item.id === selectedId" :disabled="busy" @click="selectFile(item.id)"><b>{{ item.name }}</b><small>{{ item.folder }} · v{{ item.versions.at(-1).number }}</small></button>
                <p v-if="!files.length">{{ project.files.length ? '没有满足筛选条件的文件。' : '项目还没有文件。可先添加 TXT、Markdown、CSV 或 JSON。' }}</p>
              </div>
              <div class="project-rows" v-else>
                <button type="button" v-for="item in notes" :key="item.id" :aria-pressed="item.id === selectedId" :disabled="busy" @click="selectNote(item.id)"><b>{{ item.title }}</b><small>{{ noteStatus(project, item) }}</small></button>
                <p v-if="!notes.length">{{ project.notes.length ? '没有匹配的知识卡。' : '打开一份文件并选择「整理知识卡」，为知识保留出处。' }}</p>
              </div>
            </section>
            <section class="project-detail" aria-label="资料详情">
              <template v-if="view === 'files' && source">
                <header><h2 id="project-file-heading" tabindex="-1">{{ source.name }}</h2><small>{{ source.folder }} · {{ dateLabel(version.date) }}</small></header>
                <OrderVerification v-if="showOrderVerification" :source="orderSource" :draft="orderDraft" @submitted="status = '订单字段核验已提交（本地虚构样例）。'" />
                <SubtitleCorrection v-if="showSubtitleCorrection" />
                <OutboundExpression v-if="showOutboundExpression" />
                <div class="project-actions"><label>版本 <select :value="version.number" @change="chosenVersion = Number($event.target.value)"><option v-for="v in [...source.versions].reverse()" :key="v.number" :value="v.number">v{{ v.number }}</option></select></label><button type="button" class="project-button" @click="openForm({kind:'version', sourceId:source.id, file:null})" :disabled="busy">新增版本</button><button type="button" class="project-button" @click="createNote" :disabled="busy">整理知识卡</button></div>
                <pre class="project-preview">{{ version.content }}</pre>
                <div class="project-actions"><button type="button" class="project-button" @click="ask(source.name, version.content)" :disabled="busyChat">带到对话</button><button type="button" class="project-button" @click="openForm({kind:'move', folder:source.folder})" :disabled="busy">移动到目录</button><button type="button" class="project-button danger" @click="deleteFile" :disabled="busy">移除文件</button></div>
                <div v-if="visibleNotesForSource.length" class="project-related"><h3>关联知识</h3><button type="button" class="project-button" v-for="item in visibleNotesForSource" :key="item.id" @click="selectNote(item.id)">{{ item.title }}</button></div>
              </template>
              <template v-else-if="view === 'wiki' && note">
                <header><h2>{{ note.title }}</h2><small>{{ noteStatus(project, note) }}</small></header>
                <pre class="project-preview">{{ note.body }}</pre>
                <div class="project-actions"><button v-if="hasNoteVersion" type="button" class="project-button" @click="selectFile(note.sourceId, note.sourceVersion)">查看来源（v{{ note.sourceVersion }}）</button><small v-else-if="noteSource">原版本已超过本机保留范围</small><button v-if="noteSource" type="button" class="project-button" @click="editNote" :disabled="busy">编辑／复核</button><button v-if="noteStatus(project, note) === '来源为最新版'" type="button" class="project-button" @click="ask(note.title, note.body)" :disabled="busyChat">带到对话</button><button type="button" class="project-button danger" @click="deleteNote" :disabled="busy">删除知识卡</button></div>
              </template>
              <p v-else class="project-detail-empty">选择一条资料查看正文、版本和来源。</p>
            </section>
          </div>
          <footer class="project-footer"><span>资料保存在此浏览器 · 用分享包传给队友</span><button type="button" class="project-button" @click="$emit('ingest')">接入 PDF、Word 或图片</button><button type="button" class="project-button danger" @click="deleteProject" :disabled="busy">删除本机项目</button></footer>
        </template>
      </div>
    </main>
  `,
}
