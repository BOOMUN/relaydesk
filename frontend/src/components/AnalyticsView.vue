<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { BarChart3, Bot, CircleCheckBig, MessagesSquare, UserRoundCheck } from '@lucide/vue'
import { api } from '../api'
import { useLocale } from '../i18n'
import type { DashboardData, QualityEvaluationReport } from '../types'

const data = ref<DashboardData | null>(null)
const quality = ref<QualityEvaluationReport | null>(null)
const { locale } = useLocale()
const routeMax = computed(() => Math.max(1, ...Object.values(data.value?.route_counts || {})))
const statusMax = computed(() => Math.max(1, ...Object.values(data.value?.status_counts || {})))
const labels = computed<Record<string, string>>(() => locale.value === 'zh-TW' ? {
  open: '處理中', pending: '待處理', expired: '視窗過期', solved: '已解決', blocked: '已封鎖',
  knowledge: '知識庫', order: '訂單工具', handoff: '人工接管', unclassified: '未分類',
} : {
  open: '处理中', pending: '等待客户', expired: '窗口过期', solved: '已解决', blocked: '已阻止',
  knowledge: '知识库', order: '订单工具', handoff: '人工接管', unclassified: '未分类',
})
const qualityCards = computed(() => quality.value ? [
  {
    label: locale.value === 'zh-TW' ? '意圖判斷準確率' : '意图判断准确率',
    value: quality.value.metrics.intent_accuracy_pct,
    detail: `${quality.value.intent_accuracy.correct_count}/${quality.value.intent_accuracy.case_count}`,
  },
  {
    label: locale.value === 'zh-TW' ? '國家召回率' : '国家召回率',
    value: quality.value.metrics.country_recall_pct,
    detail: `${quality.value.country_recall.hit_count}/${quality.value.country_recall.expected_count}`,
  },
  {
    label: locale.value === 'zh-TW' ? '商品召回率' : '商品召回率',
    value: quality.value.metrics.product_recall_pct,
    detail: `${quality.value.product_recall.hit_count}/${quality.value.product_recall.expected_count}`,
  },
  {
    label: locale.value === 'zh-TW' ? '檢索 Top-1 準確率' : '检索 Top-1 准确率',
    value: quality.value.metrics.retrieval_top1_accuracy_pct,
    detail: `${quality.value.retrieval_accuracy.top1_correct_count}/${quality.value.retrieval_accuracy.evaluated_cases}`,
  },
  {
    label: locale.value === 'zh-TW' ? '檢索 Top-3 準確率' : '检索 Top-3 准确率',
    value: quality.value.metrics.retrieval_top3_accuracy_pct,
    detail: `${quality.value.retrieval_accuracy.top3_correct_count}/${quality.value.retrieval_accuracy.evaluated_cases}`,
  },
] : [])

function formatEvaluationTime(value: string) {
  return new Intl.DateTimeFormat(
    locale.value === 'zh-TW' ? 'zh-TW' : 'zh-CN',
    { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' },
  ).format(new Date(value))
}

onMounted(async () => {
  data.value = await api.get<DashboardData>('/api/dashboard')
  try {
    quality.value = await api.get<QualityEvaluationReport>('/api/quality-evaluation/latest')
  } catch {
    quality.value = null
  }
})
</script>

<template>
  <div class="page-view analytics-view">
    <section class="page-toolbar"><div><p class="section-kicker">Operations analytics</p><h2>{{ locale === 'zh-TW' ? '客服表現' : '客服表现' }}</h2></div><span class="date-range">{{ locale === 'zh-TW' ? '全部示範資料' : '全部演示数据' }}</span></section>
    <section v-if="quality" class="panel quality-evaluation-panel">
      <div class="panel-heading">
        <div>
          <p class="section-kicker">Labelled agent evaluation</p>
          <h2>{{ locale === 'zh-TW' ? 'Agent 品質指標' : 'Agent 质量指标' }}</h2>
        </div>
        <span class="date-range">
          {{ formatEvaluationTime(quality.generated_at) }} · {{ quality.live_model ? 'Live model' : 'Deterministic' }}
        </span>
      </div>
      <div class="quality-metric-grid">
        <article v-for="metric in qualityCards" :key="metric.label" class="quality-metric-card">
          <span>{{ metric.label }}</span>
          <strong>{{ metric.value.toFixed(2) }}%</strong>
          <small>{{ metric.detail }}</small>
        </article>
      </div>
    </section>
    <section v-if="data" class="analytics-grid">
      <article class="panel chart-panel">
        <div class="panel-heading"><div><p class="section-kicker">Conversation state</p><h2>{{ locale === 'zh-TW' ? '會話狀態分佈' : '会话状态分布' }}</h2></div><MessagesSquare :size="20" /></div>
        <div class="bar-list">
          <div v-for="(count, status) in data.status_counts" :key="status" class="bar-row">
            <span>{{ labels[status] || status }}</span><div class="bar-track"><i :class="['bar-fill', status]" :style="{ width: `${(count / statusMax) * 100}%` }" /></div><strong>{{ count }}</strong>
          </div>
        </div>
      </article>
      <article class="panel chart-panel">
        <div class="panel-heading"><div><p class="section-kicker">Agent routing</p><h2>{{ locale === 'zh-TW' ? 'AI 路由結果' : 'AI 路由结果' }}</h2></div><Bot :size="20" /></div>
        <div class="bar-list">
          <div v-for="(count, route) in data.route_counts" :key="route" class="bar-row">
            <span>{{ labels[route] || route }}</span><div class="bar-track"><i class="bar-fill route" :style="{ width: `${(count / routeMax) * 100}%` }" /></div><strong>{{ count }}</strong>
          </div>
        </div>
      </article>
      <article class="panel analytics-note"><CircleCheckBig :size="22" /><div><strong>{{ locale === 'zh-TW' ? '已解決' : '已解决' }}</strong><span>{{ data.status_counts.solved || 0 }} {{ locale === 'zh-TW' ? '個會話' : '个会话' }}</span></div></article>
      <article class="panel analytics-note amber"><UserRoundCheck :size="22" /><div><strong>{{ locale === 'zh-TW' ? '人工接管' : '人工接管' }}</strong><span>{{ data.route_counts.handoff || 0 }} {{ locale === 'zh-TW' ? '個會話' : '个会话' }}</span></div></article>
      <article class="panel analytics-note blue"><BarChart3 :size="22" /><div><strong>{{ locale === 'zh-TW' ? '訊息總量' : '消息总量' }}</strong><span>{{ data.metrics.find((item) => item.label === '消息总量')?.value || 0 }} {{ locale === 'zh-TW' ? '則' : '条' }}</span></div></article>
    </section>
  </div>
</template>
