export const FILE_LIMIT = 64 * 1024
export const BUNDLE_LIMIT = 512 * 1024
const MAX_PROJECTS = 12
const MAX_FILES = 40
const MAX_VERSIONS = 8
const MAX_NOTES = 60
const encoder = new TextEncoder()

export const projectFileAccept = '.txt,.md,.csv,.json'
export const projectStorageKey = user => `omniable.project-library.v1:${encodeURIComponent(user)}`

function checkText(value, max, label, { singleLine = false } = {}) {
  if (typeof value !== 'string' || !value.trim() || encoder.encode(value).length > max ||
      value.includes('\0') || (singleLine && /[\u0000-\u001f\u007f]/u.test(value))) {
    throw new Error(`${label}为空、格式无效或超过长度限制。`)
  }
  return value
}

const label = (value, name) => checkText(typeof value === 'string' ? value.trim() : value, 240, name, { singleLine: true })
const uuid = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
const now = () => new Date().toISOString()
const isProjectFile = name => projectFileAccept.split(',').includes(name.toLowerCase().match(/\.[^.]+$/u)?.[0])

export function loadLibrary(storage, user) {
  const raw = storage.getItem(projectStorageKey(user))
  if (raw === null) return []
  const data = JSON.parse(raw)
  if (data?.version !== 1 || !Array.isArray(data.projects)) throw new Error('无法读取本地项目资料。')
  return data.projects
}

export function saveLibrary(storage, user, projects) {
  storage.setItem(projectStorageKey(user), JSON.stringify({ version: 1, projects }))
}

export function addProject(projects, name, id = uuid()) {
  if (projects.length >= MAX_PROJECTS) throw new Error(`本机最多保留 ${MAX_PROJECTS} 个项目。`)
  return { id, name: label(name, '项目名'), files: [], notes: [] }
}

export async function readProjectFile(file) {
  if (!isProjectFile(file.name)) throw new Error('请选择 TXT、Markdown、CSV 或 JSON 文件。')
  if (!file.size || file.size > FILE_LIMIT) throw new Error('文件需非空且不超过 64 KiB。')
  const bytes = await file.arrayBuffer()
  try {
    return checkText(new TextDecoder('utf-8', { fatal: true }).decode(bytes), FILE_LIMIT, '文件正文')
  } catch {
    throw new Error('文件必须是非空的 UTF-8 文本。')
  }
}

export function addFile(project, { name, folder, content }, id = uuid()) {
  if (project.files.length >= MAX_FILES) throw new Error(`每个项目最多保留 ${MAX_FILES} 份文件。`)
  const source = { id, name: label(name, '文件名'), folder: label(folder || '未分类', '目录'),
    versions: [{ number: 1, content: checkText(content, FILE_LIMIT, '文件正文'), date: now() }] }
  if (!isProjectFile(source.name)) throw new Error('请选择 TXT、Markdown、CSV 或 JSON 文件。')
  if (project.files.some(file => file.folder === source.folder && file.name === source.name)) {
    throw new Error('该目录已有同名文件，请打开文件后添加新版本。')
  }
  return source
}

export function moveFile(project, file, folder) {
  const destination = label(folder || '未分类', '目录')
  if (project.files.some(item => item.id !== file.id && item.folder === destination && item.name === file.name)) {
    throw new Error('目标目录已有同名文件。')
  }
  return { ...file, folder: destination }
}

export function addVersion(file, content) {
  checkText(content, FILE_LIMIT, '文件正文')
  const current = file.versions.at(-1)
  if (current.content === content) throw new Error('正文与当前版本相同。')
  if (current.number >= Number.MAX_SAFE_INTEGER) throw new Error('文件版本号已达上限。')
  return { ...file, versions: [...file.versions, { number: current.number + 1, content, date: now() }].slice(-MAX_VERSIONS) }
}

export function saveNote(project, { id, title, body, sourceId, sourceVersion }) {
  if (!id && project.notes.length >= MAX_NOTES) throw new Error(`每个项目最多保留 ${MAX_NOTES} 张知识卡。`)
  const source = project.files.find(file => file.id === sourceId)
  const version = source?.versions.find(item => item.number === sourceVersion)
  if (!version) throw new Error('请选择一份可用的来源版本。')
  return { id: id || uuid(), title: label(title, '知识标题'), body: checkText(body, 8192, '知识正文'),
    sourceId, sourceVersion }
}

export function noteStatus(project, note) {
  const source = project.files.find(file => file.id === note.sourceId)
  return !source ? '来源已移除' : source.versions.at(-1).number === note.sourceVersion ? '来源为最新版' : '来源有更新，待复核'
}

export function exportProject(project, fileIds) {
  const selected = project.files.filter(file => fileIds.includes(file.id))
  if (!selected.length) throw new Error('至少选择一份要共享的文件。')
  const files = selected.map((file, index) => {
    const { number, date, content } = file.versions.at(-1)
    return { key: index, name: file.name, folder: file.folder, number, date, content }
  })
  const notes = project.notes.flatMap(note => {
    const key = selected.findIndex(file => file.id === note.sourceId && file.versions.at(-1).number === note.sourceVersion)
    return key === -1 ? [] : [{ key, title: note.title, body: note.body }]
  })
  const data = JSON.stringify({ format: 'omniable-project', version: 1, name: project.name, files, notes }, null, 2)
  if (encoder.encode(data).length > BUNDLE_LIMIT) throw new Error('分享包超过 512 KiB，请减少文件数量。')
  return { data, fileCount: files.length, noteCount: notes.length }
}

export function importProject(projects, raw) {
  if (encoder.encode(raw).length > BUNDLE_LIMIT) throw new Error('分享包超过 512 KiB。')
  let data
  try { data = JSON.parse(raw) } catch { throw new Error('分享包不是有效的 JSON 文件。') }
  if (data?.format !== 'omniable-project' || data.version !== 1 || !Array.isArray(data.files) ||
      !data.files.length || data.files.length > MAX_FILES || !Array.isArray(data.notes) || data.notes.length > MAX_NOTES) {
    throw new Error('这不是受支持的 OmniAble 项目分享包。')
  }
  const project = addProject(projects, data.name)
  const sources = new Map()
  for (const file of data.files) {
    if (!file || !Number.isSafeInteger(file.key) || sources.has(file.key) ||
        !Number.isSafeInteger(file.number) || file.number < 1 || typeof file.date !== 'string' || !Number.isFinite(Date.parse(file.date))) {
      throw new Error('分享包中的文件标识或版本信息无效。')
    }
    const source = addFile(project, file)
    source.versions[0] = { number: file.number, date: new Date(file.date).toISOString(), content: source.versions[0].content }
    sources.set(file.key, source)
    project.files.push(source)
  }
  for (const note of data.notes) {
    const source = sources.get(note?.key)
    if (!source) throw new Error('分享包中的知识卡缺少对应文件。')
    project.notes.push(saveNote(project, { title: note.title, body: note.body, sourceId: source.id, sourceVersion: source.versions[0].number }))
  }
  return project
}
