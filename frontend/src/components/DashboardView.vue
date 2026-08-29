<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ArrowRight, BookOpenText, Bot, CircleAlert, Clock3, MessageSquareText, UserRoundCheck, Users } from '@lucide/vue'
import { api } from '../api'
import { useLocale } from '../i18n'
import type { DashboardData } from '../types'

defineEmits<{ openInbox: [] }>()
const data = ref<DashboardData | null>(null)
const loading = ref(true)
const { locale } = useLocale()

const metricIcons = [Clock3, Users, MessageSquareText, Bot, ArrowRight]
const metricLabels = computed(() => locale.value === 'zh-TW'
  ? ['待處理會話', '客戶總數', '訊息總量', 'AI 回覆', '轉人工率']
  : ['待处理会话', '客户总数', '消息总量', 'AI 回复', '转人工率'])
const statusLabel = computed<Record<string, string>>(() => locale.value === 'zh-TW' ? {
  open: '處理中', pending: '待處理', expired: '視窗過期', solved: '已解決', blocked: '已封鎖',
} : {
  open: '处理中', pending: '等待客户', expired: '窗口过期', solved: '已解决', blocked: '已阻止',
})

const traditionalMetrics: Record<string, string> = {
  待处理会话: '待處理會話', 客户总数: '客戶總數', 消息总量: '訊息總量', 'AI 回复': 'AI 回覆', 转人工率: '轉人工率',
}

const routeLabel = computed<Record<string, string>>(() => locale.value === 'zh-TW' ? {
  knowledge: '知識庫', order: '訂單工具', handoff: '人工接管', unclassified: '未分類',
} : {
  knowledge: '知识库', order: '订单工具', handoff: '人工接管', unclassified: '未分类',
})

function metricLabel(value: string) {
  return locale.value === 'zh-TW' ? traditionalMetrics[value] || value : value
}

const dashboardAlerts = computed(() => [
  {
    key: 'knowledge',
    label: locale.value === 'zh-TW' ? '待審核知識庫' : '待审核知识库',
    detail: locale.value === 'zh-TW' ? '確認新內容後再發布' : '确认新内容后再发布',
    value: '—',
    tone: 'amber',
    icon: BookOpenText,
  },
  {
    key: 'actions',
    label: locale.value === 'zh-TW' ? '失敗操作' : '失败操作',
    detail: locale.value === 'zh-TW' ? '需要重試或人工處理' : '需要重试或人工处理',
    value: '—',
    tone: 'red',
    icon: CircleAlert,
  },
  {
    key: 'handoff',
    label: locale.value === 'zh-TW' ? '人工接管' : '人工接管',
    detail: locale.value === 'zh-TW' ? '優先查看待接手會話' : '优先查看待接手会话',
    value: data.value?.route_counts.handoff || 0,
    tone: 'blue',
    icon: UserRoundCheck,
  },
])

function formatTime(value: string) {
  return new Intl.DateTimeFormat(locale.value === 'zh-TW' ? 'zh-TW' : 'zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
}

onMounted(async () => {
  try {
    data.value = await api.get<DashboardData>('/api/dashboard')
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="page-view dashboard-view">
    <div v-if="loading" class="loading-state">{{ locale === 'zh-TW' ? '正在載入工作區資料' : '正在加载工作区数据' }}</div>
    <template v-else-if="data">
      <section class="metric-grid">
        <article v-for="(metric, index) in data.metrics" :key="metric.label" :class="['metric-card', { 'metric-card-primary': index < 4, 'metric-card-secondary': index >= 4 }]">
          <div class="metric-icon"><component :is="metricIcons[index]" :size="19" /></div>
          <span>{{ metricLabels[index] || metricLabel(metric.label) }}</span>
          <strong>{{ metric.value }}{{ metric.unit || '' }}</strong>
        </article>
      </section>

      <section class="dashboard-alerts" aria-label="运营提醒">
        <article v-for="alert in dashboardAlerts" :key="alert.key" :class="['dashboard-alert', `tone-${alert.tone}`]">
          <div class="dashboard-alert-icon"><component :is="alert.icon" :size="16" /></div>
          <div>
            <strong>{{ alert.label }}</strong>
            <span>{{ alert.detail }}</span>
          </div>
          <b>{{ alert.value }}</b>
          <ArrowRight :size="15" />
        </article>
      </section>

      <div class="dashboard-columns">
        <section class="panel dashboard-main-panel">
          <div class="panel-heading">
            <div>
              <p class="section-kicker">Recent activity</p>
              <h2>{{ locale === 'zh-TW' ? '最近會話' : '最近会话' }}</h2>
            </div>
            <button class="text-button" @click="$emit('openInbox')">
              {{ locale === 'zh-TW' ? '查看收件匣' : '查看收件箱' }} <ArrowRight :size="16" />
            </button>
          </div>
          <div class="recent-list">
            <div v-for="conversation in data.recent_conversations" :key="conversation.id" class="recent-row">
              <div class="contact-avatar">{{ conversation.contact.display_name.slice(0, 1).toUpperCase() }}</div>
              <div class="recent-copy">
                <strong>{{ conversation.contact.display_name }}</strong>
                <span>{{ conversation.last_message }}</span>
              </div>
               <span :class="['status-badge', conversation.status]">{{ statusLabel[conversation.status] }}</span>
              <time>{{ formatTime(conversation.last_message_at) }}</time>
            </div>
          </div>
        </section>

        <section class="panel queue-panel">
          <div class="panel-heading">
            <div>
              <p class="section-kicker">Queue</p>
              <h2>{{ locale === 'zh-TW' ? '佇列狀態' : '队列状态' }}</h2>
            </div>
          </div>
          <div class="queue-list">
            <div v-for="(count, status) in data.status_counts" :key="status" class="queue-row">
              <span><i :class="['queue-dot', status]" />{{ statusLabel[status] || status }}</span>
              <strong>{{ count }}</strong>
            </div>
          </div>
          <div class="route-summary">
            <p>{{ locale === 'zh-TW' ? 'Agent 路由' : 'Agent 路由' }}</p>
            <div v-for="(count, route) in data.route_counts" :key="route">
              <span>{{ routeLabel[route] || route }}</span><strong>{{ count }}</strong>
            </div>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>
