const prompts = [
  {
    id: 'preferences',
    label: '梳理当前偏好',
    hint: '按场景整理生效偏好，并标出仍未确认的部分。',
    prompt: '请根据我的记忆，整理当前生效的偏好，并按场景说明哪些偏好仍需要我确认。',
  },
  {
    id: 'steps',
    label: '写成可执行步骤',
    hint: '把结论改成分步操作，先做风险最低的一步。',
    prompt: '请把刚才的结论整理成可以直接执行的步骤，先给出风险最低的一步，并说明如何验证结果。',
  },
  {
    id: 'memory-review',
    label: '回顾本轮召回',
    hint: '检查这轮回答引用了哪些记忆，是否遗漏关键信息。',
    prompt: '请回顾这轮对话实际召回的记忆，说明哪些内容影响了回答，哪些重要信息可能没有召回。',
  },
  {
    id: 'next-actions',
    label: '列出下一步',
    hint: '根据当前上下文给出三个可选下一步。',
    prompt: '请根据当前对话和我的偏好，列出三个可选的下一步，并说明每个下一步的适用场景。',
  },
]

export default {
  props: {
    messages: { type: Array, default: () => [] },
    memories: { type: Array, default: () => [] },
    user: { type: String, default: '' },
  },
  emits: ['use'],
  setup() {
    return { prompts }
  },
  template: `
    <div class="plugin-run">
      <p class="plugin-run-lead">选择一个模板插入输入框。你可以继续修改，确认后再发送。</p>
      <div class="plugin-prompt-list">
        <button v-for="item in prompts" :key="item.id" type="button" class="plugin-prompt" data-testid="plugin-prompt" @click="$emit('use', item.prompt)">
          <span class="plugin-prompt-copy"><b>{{ item.label }}</b><small>{{ item.hint }}</small></span>
          <span class="plugin-prompt-action" aria-hidden="true">插入</span>
        </button>
      </div>
    </div>
  `,
}
