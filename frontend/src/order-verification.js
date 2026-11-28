import { computed, ref } from 'vue'

export function parseOrderFields(content) {
  const read = pattern => {
    const match = String(content || '').match(pattern)
    return match ? Number(match[1]) : null
  }
  return {
    product: String(content || '').match(/^商品[：:]\s*(.+)$/mu)?.[1]?.trim() || '',
    quantity: read(/^数量[：:]\s*(\d+)/mu),
    unitPrice: read(/^单价[：:]\s*(\d+(?:\.\d+)?)/mu),
    amount: read(/^(?:金额|确认金额)[：:]\s*(\d+(?:\.\d+)?)/mu),
  }
}

export function needsRecheck(acceptedVersion, latestVersion) {
  return Number(acceptedVersion) !== Number(latestVersion)
}

export function validateOrderFields(fields, expected) {
  const errors = []
  if (!Number.isInteger(fields.quantity) || fields.quantity <= 0) {
    errors.push('核对后的数量必须是正整数。')
  }
  if (!Number.isFinite(fields.amount) || fields.amount <= 0) {
    errors.push('核对后的金额必须是正数。')
  }
  if (fields.quantity !== expected.quantity) {
    errors.push(`数量应改为 ${expected.quantity} 件。`)
  }
  if (fields.amount !== expected.amount) {
    errors.push(`金额应改为 ${expected.amount} 元。`)
  }
  if (fields.quantity !== null && fields.unitPrice !== expected.unitPrice &&
      fields.amount !== expected.quantity * expected.unitPrice) {
    errors.push('金额与数量、单价不一致。')
  }
  return errors
}

export default {
  name: 'OrderVerification',
  props: {
    source: { type: Object, required: true },
    draft: { type: Object, required: true },
  },
  emits: ['submitted'],
  setup(props, { emit }) {
    const initial = computed(() => parseOrderFields(props.draft.versions.at(-1).content))
    const quantity = ref(initial.value.quantity)
    const amount = ref(initial.value.amount)
    const acceptedVersion = ref(props.source.versions.at(-1).number)
    const receipt = ref(null)
    const message = ref('')
    const cancelled = ref(false)
    const sourceFields = computed(() => parseOrderFields(props.source.versions.at(-1).content))
    const latestVersion = computed(() => props.source.versions.at(-1).number)
    const stale = computed(() => needsRecheck(acceptedVersion.value, latestVersion.value))
    const errors = computed(() => validateOrderFields(
      { ...initial.value, quantity: quantity.value, amount: amount.value },
      sourceFields.value,
    ))
    const statusId = 'order-verification-status'

    function cancel() {
      cancelled.value = true
      message.value = receipt.value ? '本次修改已取消，保留已提交的演示结果。' : '已取消核验，没有提交任何字段。'
    }

    function adoptLatest() {
      acceptedVersion.value = latestVersion.value
      cancelled.value = false
      message.value = `已采用来源 v${latestVersion.value}，请重新核对后提交。`
    }

    function submit() {
      if (receipt.value?.sourceVersion === acceptedVersion.value) {
        message.value = '此结果已提交；重复确认不会产生第二次结果。'
        return
      }
      if (stale.value) {
        message.value = `来源已更新到 v${latestVersion.value}，请重新复核后再提交。`
        return
      }
      if (errors.value.length) {
        message.value = errors.value[0]
        return
      }
      cancelled.value = false
      message.value = '已提交 1 次核验结果；再次确认不会重复提交。'
      receipt.value = {
        orderId: 'DEMO-01',
        product: sourceFields.value.product,
        quantity: quantity.value,
        unitPrice: sourceFields.value.unitPrice,
        amount: amount.value,
        sourceVersion: acceptedVersion.value,
      }
      emit('submitted', { ...receipt.value })
    }

    return {
      quantity, amount, acceptedVersion, receipt, message, cancelled, initial,
      sourceFields, latestVersion, stale, errors, statusId, cancel, adoptLatest, submit,
    }
  },
  template: `
    <section class="order-verification" data-testid="order-verification" aria-labelledby="order-verification-heading">
      <header>
        <div>
          <h3 id="order-verification-heading">提交前字段核验</h3>
          <p>当前订单 DEMO-01 · 客户确认单 v{{ latestVersion }}</p>
        </div>
        <span class="order-verification-badge" :data-stale="stale">{{ stale ? '来源有更新，需复核' : '来源已核对' }}</span>
      </header>

      <div class="order-verification-grid">
        <section aria-labelledby="order-source-fields">
          <h4 id="order-source-fields">客户确认单</h4>
          <dl>
            <div><dt>商品</dt><dd>{{ sourceFields.product }}</dd></div>
            <div><dt>数量</dt><dd>{{ sourceFields.quantity }} 件</dd></div>
            <div><dt>单价</dt><dd>{{ sourceFields.unitPrice }} 元</dd></div>
            <div><dt>确认金额</dt><dd>{{ sourceFields.amount }} 元</dd></div>
            <div><dt>来源</dt><dd>客户确认单 v{{ latestVersion }}</dd></div>
          </dl>
        </section>
        <form @submit.prevent="submit">
          <h4>核对后提交</h4>
          <p>订单原始字段：商品 {{ initial.product }}，数量 {{ initial.quantity }} 件，单价 {{ initial.unitPrice }} 元，金额 {{ initial.amount }} 元。</p>
          <label for="order-quantity">核对后数量（件）</label>
          <input id="order-quantity" v-model.number="quantity" type="number" min="1" step="1" inputmode="numeric" aria-describedby="order-verification-status" />
          <label for="order-amount">核对后金额（元）</label>
          <input id="order-amount" v-model.number="amount" type="number" min="1" step="0.01" inputmode="decimal" aria-describedby="order-verification-status" />
          <p class="order-verification-source">本次采用来源 v{{ acceptedVersion }} · 单价 {{ sourceFields.unitPrice }} 元</p>
          <p v-if="stale" class="order-verification-alert" role="alert">来源已从 v{{ acceptedVersion }} 更新到 v{{ latestVersion }}，必须重新核对后才能提交。</p>
          <p v-if="errors.length && !cancelled && !stale" class="order-verification-alert" role="alert">{{ errors[0] }}</p>
          <p class="order-verification-status" :id="statusId" role="status" aria-live="polite">{{ message }}</p>
          <p v-if="receipt" class="order-verification-result" data-testid="order-verification-result">
            已提交 1 次：{{ receipt.product }}，{{ receipt.quantity }} 件，{{ receipt.amount }} 元，来源 v{{ receipt.sourceVersion }}。重复确认不会产生第二次结果。
          </p>
          <div class="project-actions">
            <button type="submit" class="project-button primary">提交核验</button>
            <button type="button" class="project-button" @click="cancel">取消核验</button>
            <button v-if="stale" type="button" class="project-button" @click="adoptLatest">采用最新来源并复核</button>
          </div>
        </form>
      </div>
    </section>
  `,
}
