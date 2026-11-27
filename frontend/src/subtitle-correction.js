import { computed, ref } from 'vue'

export const subtitleSample = {
  transcript: '客户要蓝色收纳袋两件，单价二十八元。',
  confirmedProduct: '蓝色收纳盒',
  confirmedQuantity: 3,
  unitPrice: 28,
}

export function correctionDiff(business, correction) {
  const changes = []
  if (business.product !== correction.product) {
    changes.push({ field: '商品', before: business.product, after: correction.product })
  }
  if (business.quantity !== correction.quantity) {
    changes.push({ field: '数量', before: business.quantity, after: correction.quantity })
  }
  return changes
}

export function applySubtitleCorrection(business, correction) {
  const changes = correctionDiff(business, correction)
  return {
    business: { ...business, product: correction.product, quantity: correction.quantity },
    changes,
  }
}

export default {
  name: 'SubtitleCorrection',
  setup() {
    const transcript = ref(subtitleSample.transcript)
    const business = ref({ product: '蓝色收纳袋', quantity: 2 })
    const product = ref(subtitleSample.confirmedProduct)
    const quantity = ref(subtitleSample.confirmedQuantity)
    const textMode = ref(false)
    const history = ref([])
    const message = ref('')
    const statusId = 'subtitle-correction-status'
    const changes = computed(() => correctionDiff(business.value, {
      product: product.value, quantity: quantity.value,
    }))
    const changed = computed(() => changes.value.length > 0)

    function toggleTextMode() {
      textMode.value = !textMode.value
      message.value = textMode.value ? '已切换到文字输入；没有请求麦克风权限。' : '已切回模拟语音转写。'
    }

    function cancel() {
      message.value = '已取消更正，业务字段保持原值。'
    }

    function confirm() {
      if (!changed.value) {
        message.value = '没有待应用的更正，业务字段保持不变。'
        return
      }
      const applied = applySubtitleCorrection(business.value, {
        product: product.value, quantity: quantity.value,
      })
      history.value.push({
        id: history.value.length + 1,
        before: { ...business.value },
        after: { ...applied.business },
        changes: applied.changes,
        source: '模拟语音转写 + 文字更正',
      })
      business.value = applied.business
      message.value = `已确认更正：${applied.changes.map(item => `${item.field} ${item.before} → ${item.after}`).join('；')}。`
    }

    function historyText(entry) {
      return entry.changes.map(item => `${item.field} ${item.before} → ${item.after}`).join('；')
    }

    return {
      transcript, business, product, quantity, textMode, history, message, statusId,
      changes, changed, toggleTextMode, cancel, confirm, historyText, unitPrice: subtitleSample.unitPrice,
    }
  },
  template: `
    <section class="subtitle-correction" data-testid="subtitle-correction" aria-labelledby="subtitle-correction-heading">
      <header>
        <div>
          <h3 id="subtitle-correction-heading">字幕纠错与业务确认</h3>
          <p>模拟语音转写 · 未请求麦克风权限 · 文字回退可用</p>
        </div>
        <span class="subtitle-badge">{{ textMode ? '文字输入' : '模拟转写' }}</span>
      </header>

      <section aria-labelledby="subtitle-original">
        <h4 id="subtitle-original">原转写</h4>
        <pre>{{ transcript }}</pre>
        <button type="button" class="project-button" @click="toggleTextMode">
          {{ textMode ? '收起文字输入' : '无麦克风？切换到文字输入' }}
        </button>
        <label v-if="textMode" for="subtitle-text-input">可编辑文字转写</label>
        <textarea v-if="textMode" id="subtitle-text-input" v-model="transcript" rows="3" aria-describedby="subtitle-correction-status"></textarea>
      </section>

      <div class="subtitle-grid">
        <section aria-labelledby="subtitle-business">
          <h4 id="subtitle-business">当前业务字段（尚未确认）</h4>
          <dl>
            <div><dt>商品</dt><dd data-testid="subtitle-business-product">{{ business.product }}</dd></div>
            <div><dt>数量</dt><dd data-testid="subtitle-business-quantity">{{ business.quantity }} 件</dd></div>
            <div><dt>单价</dt><dd>{{ unitPrice }} 元</dd></div>
          </dl>
        </section>

        <form @submit.prevent="confirm">
          <h4>更正内容</h4>
          <label for="subtitle-corrected-product">更正后的商品</label>
          <input id="subtitle-corrected-product" v-model.trim="product" maxlength="80" aria-describedby="subtitle-correction-status" />
          <label for="subtitle-corrected-quantity">更正后的数量（件）</label>
          <input id="subtitle-corrected-quantity" v-model.number="quantity" type="number" min="1" step="1" inputmode="numeric" aria-describedby="subtitle-correction-status" />
          <p v-if="changed" class="subtitle-changes" data-testid="subtitle-changes">
            待确认：<span v-for="item in changes" :key="item.field">{{ item.field }} {{ item.before }} → {{ item.after }}；</span>
          </p>
          <p v-else class="subtitle-changes">没有待应用的更正。</p>
          <p :id="statusId" class="subtitle-status" role="status" aria-live="polite">{{ message }}</p>
          <div class="project-actions">
            <button type="submit" class="project-button primary">确认更正并写入业务字段</button>
            <button type="button" class="project-button" @click="cancel">取消</button>
          </div>
        </form>
      </div>

      <section v-if="history.length" class="subtitle-history" aria-labelledby="subtitle-history">
        <h4 id="subtitle-history">更正记录</h4>
        <ol>
          <li v-for="entry in history" :key="entry.id" data-testid="subtitle-history-entry">
            第 {{ entry.id }} 次：{{ historyText(entry) }}；{{ entry.source }}
          </li>
        </ol>
      </section>
    </section>
  `,
}