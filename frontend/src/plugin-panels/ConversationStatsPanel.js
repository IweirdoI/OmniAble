import { computed } from 'vue'

export default {
  props: {
    messages: { type: Array, default: () => [] },
    memories: { type: Array, default: () => [] },
    user: { type: String, default: '' },
  },
  setup(props) {
    const stats = computed(() => {
      const userMessages = props.messages.filter(message => message.role === 'user').length
      const assistantMessages = props.messages.filter(message => message.role === 'assistant').length
      const characters = props.messages.reduce((sum, message) => sum + String(message.content || '').length, 0)
      return { userMessages, assistantMessages, characters, memories: props.memories.length }
    })
    return { stats }
  },
  template: `
    <div class="plugin-run">
      <div class="plugin-stat-grid">
        <div><span>你的消息</span><b>{{ stats.userMessages }}</b></div>
        <div><span>助理回复</span><b>{{ stats.assistantMessages }}</b></div>
        <div><span>文本字符</span><b>{{ stats.characters }}</b></div>
        <div><span>本轮召回</span><b>{{ stats.memories }}</b></div>
      </div>
      <p class="plugin-run-lead">这些数字只描述当前临时会话，不代表长期记忆库的总量，也不会触发额外检索。</p>
    </div>
  `,
}
