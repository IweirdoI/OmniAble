import { computed, ref } from 'vue'

export const outboundSample = {
  intent: '告诉客户：蓝色收纳盒 3 件，单价 28 元，合计 84 元，周五前发货。',
  draft: '您好，蓝色收纳盒共 3 件，单价 28 元，合计 84 元。我们会在周五前安排发货。',
}

export function needsConfirmation(confirmedVersion, draftVersion) {
  return Number(confirmedVersion) !== Number(draftVersion)
}

export function canSend(state) {
  return !needsConfirmation(state.confirmedVersion, state.draftVersion) &&
    Boolean(state.acknowledged) && Number(state.sentVersion) !== Number(state.confirmedVersion)
}

export default {
  name: 'OutboundExpression',
  setup() {
    const draft = ref(outboundSample.draft)
    const draftVersion = ref(1)
    const confirmedVersion = ref(0)
    const sentVersion = ref(0)
    const acknowledged = ref(false)
    const cancelled = ref(false)
    const speechStopped = ref(false)
    const message = ref('语音不可用，已使用文字流程。')
    const statusId = 'outbound-expression-status'
    const needsConfirm = computed(() => needsConfirmation(confirmedVersion.value, draftVersion.value))
    const sendable = computed(() => canSend({
      confirmedVersion: confirmedVersion.value,
      draftVersion: draftVersion.value,
      acknowledged: acknowledged.value,
      sentVersion: sentVersion.value,
    }))

    function edited() {
      draftVersion.value += 1
      acknowledged.value = false
      cancelled.value = false
      message.value = `草稿已修改为 v${draftVersion.value}，请重新确认价格和发货承诺。`
    }

    function confirmDraft() {
      if (!acknowledged.value) {
        message.value = '请先勾选“我确认价格与发货承诺”。'
        return
      }
      confirmedVersion.value = draftVersion.value
      message.value = `已确认草稿 v${confirmedVersion.value}，可以模拟外发。`
    }

    function cancel() {
      cancelled.value = true
      speechStopped.value = true
      message.value = '已取消：不播报，也不外发。'
    }

    function send() {
      if (sentVersion.value === confirmedVersion.value) {
        message.value = `v${confirmedVersion.value} 已发送；重复确认不会再次外发。`
        return
      }
      if (needsConfirm.value) {
        message.value = `草稿 v${draftVersion.value} 尚未确认，不能外发。`
        return
      }
      if (!acknowledged.value) {
        message.value = '请先确认价格与发货承诺。'
        return
      }
      sentVersion.value = confirmedVersion.value
      cancelled.value = false
      message.value = `已在本地模拟外发 v${confirmedVersion.value}；没有真实网络请求。`
    }

    function stopSpeech() {
      speechStopped.value = true
      message.value = '已停止播报（本样例没有真实语音）。文字流程仍可继续。'
    }

    return {
      intent: outboundSample.intent,
      draft, draftVersion, confirmedVersion, sentVersion, acknowledged, cancelled,
      speechStopped, message, statusId, needsConfirm, sendable,
      edited, confirmDraft, cancel, send, stopSpeech,
    }
  },
  template: `
    <section class="outbound-expression" data-testid="outbound-expression" aria-labelledby="outbound-expression-heading">
      <header>
        <div>
          <h3 id="outbound-expression-heading">对外表达审核（本地模拟）</h3>
          <p>原始意图 → 可审核草稿 → 本人确认 → 模拟外发</p>
        </div>
        <span class="outbound-badge">语音不可用 · 文字流程</span>
      </header>

      <section aria-labelledby="outbound-intent">
        <h4 id="outbound-intent">原始意图</h4>
        <pre data-testid="outbound-intent">{{ intent }}</pre>
      </section>

      <form @submit.prevent="send">
        <label for="outbound-draft">待发送草稿（v{{ draftVersion }}）</label>
        <textarea id="outbound-draft" v-model="draft" rows="3" @input="edited" aria-describedby="outbound-expression-status"></textarea>
        <p class="outbound-highlight">价格：84 元；承诺：周五前发货。<strong>这两项必须由本人确认。</strong></p>
        <label class="outbound-check">
          <input v-model="acknowledged" type="checkbox" />
          我确认价格与发货承诺，并允许发送当前版本
        </label>
        <p :id="statusId" class="outbound-status" role="status" aria-live="polite">{{ message }}</p>
        <p v-if="sentVersion" class="outbound-result" data-testid="outbound-result">
          已模拟外发 v{{ sentVersion }}；重复确认不会再次发送。
        </p>
        <div class="project-actions">
          <button type="button" class="project-button" @click="confirmDraft">确认草稿 v{{ draftVersion }}</button>
          <button type="submit" class="project-button primary" :disabled="!sendable">模拟外发</button>
          <button type="button" class="project-button" @click="cancel">取消</button>
          <button type="button" class="project-button" @click="stopSpeech">停止播报</button>
        </div>
      </form>
    </section>
  `,
}