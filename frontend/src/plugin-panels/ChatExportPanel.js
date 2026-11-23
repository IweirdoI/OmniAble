import { computed, ref } from 'vue'

const roleLabel = role => role === 'assistant' ? 'OmniAble 助理' : '你'

export default {
  props: {
    messages: { type: Array, default: () => [] },
    memories: { type: Array, default: () => [] },
    user: { type: String, default: '' },
  },
  setup(props) {
    const status = ref('')
    const messageCount = computed(() => props.messages.length)
    const characterCount = computed(() => props.messages.reduce((sum, message) => sum + String(message.content || '').length, 0))
    const download = () => {
      if (!props.messages.length) return
      const sections = props.messages.map(message => {
        const meta = [roleLabel(message.role), message.time].filter(Boolean).join(' · ')
        return `## ${meta}\n\n${message.content || ''}`
      })
      const body = `# OmniAble 对话记录\n\n导出时间：${new Date().toLocaleString('zh-CN')}\n\n${sections.join('\n\n---\n\n')}\n`
      const blob = new Blob([body], { type: 'text/markdown;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `omnidable-chat-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.md`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
      status.value = `已导出 ${props.messages.length} 条消息`
    }
    return { status, messageCount, characterCount, download }
  },
  template: `
    <div class="plugin-run">
      <div class="plugin-stat-grid">
        <div><span>当前消息</span><b>{{ messageCount }}</b></div>
        <div><span>文本字符</span><b>{{ characterCount }}</b></div>
      </div>
      <p class="plugin-run-lead">导出只包含当前页面中的临时会话；不会上传文件，也不会把内容写入新的记忆。</p>
      <button class="plugin-primary" type="button" data-testid="plugin-export" :disabled="!messageCount" @click="download">下载 Markdown</button>
      <p class="plugin-run-status" role="status">{{ status }}</p>
    </div>
  `,
}
