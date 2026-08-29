<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  Bot,
  CheckCircle2,
  CircleOff,
  Copy,
  FileCheck2,
  KeyRound,
  MessageSquareText,
  Pencil,
  Plus,
  QrCode,
  RefreshCw,
  Server,
  ShieldCheck,
  Smartphone,
  Trash2,
  TriangleAlert,
  Users,
  Webhook,
  X,
} from '@lucide/vue'
import { api } from '../api'
import { useLocale } from '../i18n'
import type {
  Agent,
  Bootstrap,
  ChannelAccount,
  QuickReply,
  RestActionEndpoint,
  Team,
  WhatsAppConnection,
  WhatsAppTemplate,
  WhatsAppTemplateSyncRun,
  WorkspaceInfo,
} from '../types'

const props = defineProps<{ session: Bootstrap }>()
const { locale, t } = useLocale()
const teams = ref<Team[]>([])
const agents = ref<Agent[]>([])
const workspace = ref<WorkspaceInfo | null>(null)
const quickReplies = ref<QuickReply[]>([])
const replyModal = ref(false)
const editingReply = ref<QuickReply | null>(null)
const savingReply = ref(false)
const replyError = ref('')
const replyForm = ref({ shortcut: '', title: '', body: '', language: 'zh-TW' })
const connection = ref<WhatsAppConnection | null>(null)
const connectionError = ref('')
const connecting = ref(false)
const copied = ref(false)
const channelAccounts = ref<ChannelAccount[]>([])
const templates = ref<WhatsAppTemplate[]>([])
const templateSyncRuns = ref<WhatsAppTemplateSyncRun[]>([])
const templateError = ref('')
const syncingTemplates = ref(false)
const restEndpoints = ref<RestActionEndpoint[]>([])
const restModal = ref(false)
const editingRest = ref<RestActionEndpoint | null>(null)
const savingRest = ref(false)
const restActionId = ref<number | null>(null)
const restError = ref('')
const restForm = ref({
  name: '',
  description: '',
  base_url: '',
  path_pattern: '/v1/*',
  allowed_methods: ['GET'] as string[],
  timeout_seconds: 10,
  requires_identity_verification: false,
  secret_header_name: '',
  secret_value: '',
  clear_secret: false,
})
let pollTimer: number | undefined

const providerName = computed(() => ({
  demo: locale.value === 'zh-TW' ? '示範通道' : '演示通道',
  meta: 'WhatsApp Cloud API',
  evolution: 'WhatsApp Web / Evolution',
})[props.session.integration.whatsapp_provider])

const stateLabel = computed(() => ({
  demo: locale.value === 'zh-TW' ? '示範模式' : '演示模式',
  not_configured: locale.value === 'zh-TW' ? '未設定' : '未配置',
  not_created: locale.value === 'zh-TW' ? '等待初始化' : '等待初始化',
  unavailable: locale.value === 'zh-TW' ? '服務不可用' : '服务不可用',
  configured: locale.value === 'zh-TW' ? '已設定' : '已配置',
  connecting: connection.value?.qr_code ? (locale.value === 'zh-TW' ? '等待掃碼' : '等待扫码') : (locale.value === 'zh-TW' ? '連線中' : '连接中'),
  disconnected: locale.value === 'zh-TW' ? '已中斷' : '已断开',
  connected: locale.value === 'zh-TW' ? '已連線' : '已连接',
})[connection.value?.state || 'not_configured'] || connection.value?.state || (locale.value === 'zh-TW' ? '檢查中' : '检查中'))

const stateClass = computed(() => connection.value?.state === 'connected' ? 'connected' : 'not-connected')
const canManageReplies = computed(() => ['admin', 'manager'].includes(props.session.user.role))
const canManageTemplates = computed(() => ['admin', 'manager'].includes(props.session.user.role))
const canManageRest = computed(() => props.session.user.role === 'admin')
const webhookUrl = computed(() => connection.value?.webhook_url || `${window.location.origin}/api/webhooks/evolution`)

async function copyWebhook() {
  await navigator.clipboard.writeText(webhookUrl.value)
  copied.value = true
  window.setTimeout(() => { copied.value = false }, 1600)
}

async function loadConnection() {
  try {
    connection.value = await api.get<WhatsAppConnection>('/api/integrations/whatsapp')
    connectionError.value = connection.value.message || ''
  } catch (error) {
    connectionError.value = error instanceof Error ? error.message : (locale.value === 'zh-TW' ? '無法讀取連線狀態' : '无法读取连接状态')
  }
}

function formatSyncTime(value: string) {
  return new Intl.DateTimeFormat(locale.value === 'zh-TW' ? 'zh-TW' : 'zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

async function loadChannelData() {
  templateError.value = ''
  try {
    const [accounts, templateRows, syncRows] = await Promise.all([
      api.get<ChannelAccount[]>('/api/channels/accounts'),
      api.get<WhatsAppTemplate[]>('/api/whatsapp/templates?active_only=false'),
      api.get<WhatsAppTemplateSyncRun[]>('/api/whatsapp/templates/sync-runs'),
    ])
    channelAccounts.value = accounts
    templates.value = templateRows
    templateSyncRuns.value = syncRows
  } catch (error) {
    templateError.value = error instanceof Error ? error.message : '無法讀取 WhatsApp 模板'
  }
}

async function syncTemplates() {
  if (syncingTemplates.value) return
  const metaAccount = channelAccounts.value.find((item) => item.provider === 'meta' && item.is_active)
  syncingTemplates.value = true
  templateError.value = ''
  try {
    await api.post('/api/whatsapp/templates/sync', {
      channel_account_id: metaAccount?.id,
    })
  } catch (error) {
    templateError.value = error instanceof Error ? error.message : '模板同步失敗'
  } finally {
    syncingTemplates.value = false
    await loadChannelData()
  }
}

async function loadRestEndpoints() {
  if (!canManageRest.value) return
  try {
    restEndpoints.value = await api.get<RestActionEndpoint[]>('/api/automation/rest-endpoints')
  } catch (error) {
    restError.value = error instanceof Error ? error.message : '無法讀取 REST Actions'
  }
}

function openRestEditor(item?: RestActionEndpoint) {
  editingRest.value = item || null
  restError.value = ''
  restForm.value = item ? {
    name: item.name,
    description: item.description,
    base_url: item.base_url,
    path_pattern: item.path_pattern,
    allowed_methods: [...item.allowed_methods],
    timeout_seconds: item.timeout_seconds,
    requires_identity_verification: item.requires_identity_verification,
    secret_header_name: item.secret_header_name || '',
    secret_value: '',
    clear_secret: false,
  } : {
    name: '',
    description: '',
    base_url: '',
    path_pattern: '/v1/*',
    allowed_methods: ['GET'],
    timeout_seconds: 10,
    requires_identity_verification: false,
    secret_header_name: '',
    secret_value: '',
    clear_secret: false,
  }
  restModal.value = true
}

function toggleRestMethod(method: string) {
  const values = restForm.value.allowed_methods
  restForm.value.allowed_methods = values.includes(method)
    ? values.filter((item) => item !== method)
    : [...values, method]
}

async function saveRestEndpoint() {
  if (!restForm.value.allowed_methods.length) {
    restError.value = locale.value === 'zh-TW' ? '至少選擇一個 HTTP 方法' : '至少选择一个 HTTP 方法'
    return
  }
  savingRest.value = true
  restError.value = ''
  const payload: Record<string, unknown> = {
    name: restForm.value.name,
    description: restForm.value.description,
    base_url: restForm.value.base_url,
    path_pattern: restForm.value.path_pattern,
    allowed_methods: restForm.value.allowed_methods,
    timeout_seconds: restForm.value.timeout_seconds,
    requires_identity_verification: restForm.value.requires_identity_verification,
    clear_secret: restForm.value.clear_secret,
  }
  if (restForm.value.secret_header_name.trim()) payload.secret_header_name = restForm.value.secret_header_name.trim()
  if (restForm.value.secret_value) payload.secret_value = restForm.value.secret_value
  try {
    if (editingRest.value) await api.patch(`/api/automation/rest-endpoints/${editingRest.value.id}`, payload)
    else await api.post('/api/automation/rest-endpoints', payload)
    restModal.value = false
    await loadRestEndpoints()
  } catch (error) {
    restError.value = error instanceof Error ? error.message : 'REST Action 儲存失敗'
  } finally {
    savingRest.value = false
  }
}

async function approveRestEndpoint(item: RestActionEndpoint) {
  restActionId.value = item.id
  restError.value = ''
  try {
    await api.post(`/api/automation/rest-endpoints/${item.id}/approve`)
    await loadRestEndpoints()
  } catch (error) {
    restError.value = error instanceof Error ? error.message : 'REST Action 審批失敗'
  } finally {
    restActionId.value = null
  }
}

async function disableRestEndpoint(item: RestActionEndpoint) {
  restActionId.value = item.id
  restError.value = ''
  try {
    await api.post(`/api/automation/rest-endpoints/${item.id}/disable`)
    await loadRestEndpoints()
  } catch (error) {
    restError.value = error instanceof Error ? error.message : 'REST Action 停用失敗'
  } finally {
    restActionId.value = null
  }
}

async function connectWhatsApp() {
  connecting.value = true
  connectionError.value = ''
  try {
    connection.value = await api.post<WhatsAppConnection>('/api/integrations/whatsapp/connect')
  } catch (error) {
    connectionError.value = error instanceof Error ? error.message : (locale.value === 'zh-TW' ? '無法建立 WhatsApp 連線' : '无法创建 WhatsApp 连接')
  } finally {
    connecting.value = false
  }
}

function openReplyEditor(item?: QuickReply) {
  replyError.value = ''
  editingReply.value = item || null
  replyForm.value = item
    ? { shortcut: item.shortcut, title: item.title, body: item.body, language: item.language }
    : { shortcut: '', title: '', body: '', language: locale.value }
  replyModal.value = true
}

async function loadQuickReplies() {
  quickReplies.value = await api.get<QuickReply[]>('/api/quick-replies')
}

async function saveQuickReply() {
  savingReply.value = true
  replyError.value = ''
  try {
    if (editingReply.value) {
      await api.patch(`/api/quick-replies/${editingReply.value.id}`, replyForm.value)
    } else {
      await api.post('/api/quick-replies', replyForm.value)
    }
    replyModal.value = false
    await loadQuickReplies()
  } catch (error) {
    replyError.value = error instanceof Error ? error.message : (locale.value === 'zh-TW' ? '無法儲存快速回覆' : '无法保存快捷回复')
  } finally {
    savingReply.value = false
  }
}

async function removeQuickReply(item: QuickReply) {
  const prompt = locale.value === 'zh-TW' ? `停用快速回覆「${item.title}」？` : `停用快捷回复“${item.title}”？`
  if (!window.confirm(prompt)) return
  await api.delete(`/api/quick-replies/${item.id}`)
  await loadQuickReplies()
}

onMounted(async () => {
  await Promise.all([
    loadConnection(),
    api.get<Team[]>('/api/teams').then((value) => { teams.value = value }),
    api.get<Agent[]>('/api/agents').then((value) => { agents.value = value }),
    api.get<WorkspaceInfo>('/api/workspace').then((value) => { workspace.value = value }),
    loadQuickReplies(),
    loadChannelData(),
    loadRestEndpoints(),
  ])
  pollTimer = window.setInterval(loadConnection, 5000)
})

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="page-view settings-view">
    <section class="page-toolbar"><div><p class="section-kicker">Workspace settings</p><h2>{{ locale === 'zh-TW' ? '連線與團隊' : '连接与团队' }}</h2></div></section>
    <div class="settings-grid">
      <section class="panel settings-panel">
        <div class="settings-heading"><Server :size="20" /><div><h3>{{ locale === 'zh-TW' ? '執行環境' : '运行环境' }}</h3><span>{{ session.integration.mode === 'live' ? (locale === 'zh-TW' ? '已設定外部通道' : '已配置外部通道') : (locale === 'zh-TW' ? '本機示範' : '本地演示') }}</span></div></div>
        <div class="setting-status"><span><Bot :size="17" />Sub2API / GPT</span><strong :class="session.integration.openai ? 'connected' : 'not-connected'"><component :is="session.integration.openai ? CheckCircle2 : CircleOff" :size="16" />{{ session.integration.openai ? (locale === 'zh-TW' ? '已設定' : '已配置') : (locale === 'zh-TW' ? '本機模型' : '本地模型') }}</strong></div>
        <div class="setting-status"><span><Smartphone :size="17" />{{ providerName }}</span><strong :class="stateClass"><component :is="connection?.state === 'connected' ? CheckCircle2 : CircleOff" :size="16" />{{ stateLabel }}</strong></div>
      </section>

      <section class="panel settings-panel connection-panel">
        <div class="settings-heading"><QrCode :size="20" /><div><h3>WhatsApp Web</h3><span>{{ connection?.instance_name || 'agentdesk' }}</span></div></div>
        <div v-if="connection?.qr_code" class="qr-stage">
          <img :src="connection.qr_code" :alt="locale === 'zh-TW' ? 'WhatsApp 關聯二維碼' : 'WhatsApp 关联二维码'" width="220" height="220" />
          <span>{{ locale === 'zh-TW' ? '等待手機確認' : '等待手机确认' }}</span>
        </div>
        <div v-else class="connection-summary">
          <Smartphone :size="31" />
          <strong>{{ stateLabel }}</strong>
          <span>{{ connection?.state === 'connected' ? (locale === 'zh-TW' ? '關聯裝置會話正常' : '关联设备会话正常') : (locale === 'zh-TW' ? '尚未建立關聯裝置會話' : '尚未建立关联设备会话') }}</span>
        </div>
        <p v-if="connectionError" class="settings-error">{{ connectionError }}</p>
        <div class="connection-actions">
          <button class="secondary-button" type="button" :title="locale === 'zh-TW' ? '重新整理連線狀態' : '刷新连接状态'" @click="loadConnection"><RefreshCw :size="16" />{{ locale === 'zh-TW' ? '重新整理' : '刷新' }}</button>
          <button v-if="connection?.state !== 'connected'" class="primary-button" type="button" :disabled="connecting || session.integration.whatsapp_provider !== 'evolution'" @click="connectWhatsApp"><QrCode :size="16" />{{ connecting ? (locale === 'zh-TW' ? '正在連線' : '正在连接') : (locale === 'zh-TW' ? '取得二維碼' : '获取二维码') }}</button>
        </div>
      </section>

      <section class="panel settings-panel full-span">
        <div class="settings-heading"><Webhook :size="20" /><div><h3>{{ locale === 'zh-TW' ? '訊息回呼' : '消息回调' }}</h3><span>Evolution {{ locale === 'zh-TW' ? '到' : '到' }} RelayDesk</span></div></div>
        <div class="copy-field"><code>{{ webhookUrl }}</code><button class="icon-button" :title="locale === 'zh-TW' ? '複製位址' : '复制地址'" @click="copyWebhook"><CheckCircle2 v-if="copied" :size="17" /><Copy v-else :size="17" /></button></div>
        <div class="config-list"><code>AGENTDESK_OPENAI_BASE_URL</code><code>AGENTDESK_EVOLUTION_API_URL</code><code>AGENTDESK_EVOLUTION_INSTANCE_NAME</code><code>AGENTDESK_EVOLUTION_WEBHOOK_SECRET</code></div>
      </section>

      <section v-if="canManageRest" class="panel settings-panel full-span rest-action-panel">
        <div class="settings-heading settings-heading-actions">
          <ShieldCheck :size="20" />
          <div><h3>REST API Actions</h3><span>{{ restEndpoints.filter((item) => item.status === 'approved').length }} approved</span></div>
          <button class="primary-button compact-button" type="button" @click="openRestEditor()"><Plus :size="15" />{{ locale === 'zh-TW' ? '新增連接器' : '新增连接器' }}</button>
        </div>
        <p v-if="restError && !restModal" class="settings-error"><TriangleAlert :size="15" />{{ restError }}</p>
        <div class="rest-endpoint-table">
          <div class="rest-endpoint-head"><span>{{ locale === 'zh-TW' ? '名稱' : '名称' }}</span><span>Origin</span><span>Path / Methods</span><span>{{ locale === 'zh-TW' ? '密鑰' : '密钥' }}</span><span>{{ locale === 'zh-TW' ? '狀態' : '状态' }}</span><span /></div>
          <div v-for="item in restEndpoints" :key="item.id" class="rest-endpoint-row">
            <div><strong>{{ item.name }}</strong><small v-if="item.requires_identity_verification"><KeyRound :size="12" />Identity required</small></div>
            <code>{{ item.base_url }}</code>
            <div><code>{{ item.path_pattern }}</code><small>{{ item.allowed_methods.join(' · ') }}</small></div>
            <span>{{ item.has_secret ? `${item.secret_header_name} · ${item.secret_fingerprint}` : '-' }}</span>
            <span :class="['template-status', item.status]">{{ item.status }}</span>
            <div class="rest-row-actions"><button class="icon-button compact" type="button" :title="locale === 'zh-TW' ? '編輯' : '编辑'" @click="openRestEditor(item)"><Pencil :size="14" /></button><button v-if="item.status !== 'approved'" class="icon-button compact" type="button" :title="locale === 'zh-TW' ? '批准' : '批准'" :disabled="restActionId === item.id" @click="approveRestEndpoint(item)"><CheckCircle2 :size="14" /></button><button v-else class="icon-button compact danger" type="button" :title="locale === 'zh-TW' ? '停用' : '停用'" :disabled="restActionId === item.id" @click="disableRestEndpoint(item)"><CircleOff :size="14" /></button></div>
          </div>
          <div v-if="!restEndpoints.length" class="empty-state compact-state">{{ locale === 'zh-TW' ? '尚無 REST Action 連接器' : '暂无 REST Action 连接器' }}</div>
        </div>
      </section>

      <section class="panel settings-panel full-span whatsapp-template-panel">
        <div class="settings-heading settings-heading-actions">
          <FileCheck2 :size="20" />
          <div><h3>WhatsApp {{ locale === 'zh-TW' ? '訊息範本' : '消息模板' }}</h3><span>Meta Business Manager</span></div>
          <button
            v-if="canManageTemplates"
            class="secondary-button compact-button"
            type="button"
            :disabled="syncingTemplates || !channelAccounts.some((item) => item.provider === 'meta')"
            @click="syncTemplates"
          ><RefreshCw :size="15" :class="{ spin: syncingTemplates }" />{{ locale === 'zh-TW' ? '同步' : '同步' }}</button>
        </div>
        <p v-if="templateError" class="settings-error"><TriangleAlert :size="15" />{{ templateError }}</p>
        <div class="channel-account-strip">
          <div v-for="account in channelAccounts" :key="account.id">
            <span :class="['provider-mark', account.provider]">{{ account.provider.toUpperCase() }}</span>
            <strong>{{ account.name }}</strong>
            <small>{{ account.phone_number_id || account.instance_name || account.external_account_id }}</small>
            <em :class="account.is_active ? 'connected' : 'not-connected'">{{ account.is_active ? (locale === 'zh-TW' ? '啟用' : '启用') : (locale === 'zh-TW' ? '停用' : '停用') }}</em>
          </div>
        </div>
        <div class="template-table" role="table" aria-label="WhatsApp templates">
          <div class="template-table-head" role="row">
            <span>{{ locale === 'zh-TW' ? '名稱' : '名称' }}</span><span>{{ locale === 'zh-TW' ? '語言' : '语言' }}</span><span>{{ locale === 'zh-TW' ? '類別' : '类别' }}</span><span>{{ locale === 'zh-TW' ? '審核狀態' : '审核状态' }}</span><span>{{ locale === 'zh-TW' ? '同步時間' : '同步时间' }}</span>
          </div>
          <div v-for="item in templates" :key="item.id" class="template-table-row" role="row">
            <strong>{{ item.name }}</strong><span>{{ item.language }}</span><span>{{ item.category }}</span><span :class="['template-status', item.status.toLowerCase()]">{{ item.status }}</span><time>{{ formatSyncTime(item.last_synced_at) }}</time>
            <p v-if="item.rejection_reason"><TriangleAlert :size="13" />{{ item.rejection_reason }}</p>
          </div>
          <div v-if="!templates.length" class="empty-state compact-state">{{ locale === 'zh-TW' ? '尚無已同步範本' : '暂无已同步模板' }}</div>
        </div>
        <div v-if="templateSyncRuns.length" class="template-sync-history">
          <div v-for="run in templateSyncRuns.slice(0, 5)" :key="run.id">
            <span :class="['sync-state-dot', run.status]" />
            <strong>{{ run.status }}</strong>
            <span>{{ run.approved_count }}/{{ run.template_count }} approved</span>
            <time>{{ formatSyncTime(run.started_at) }}</time>
            <small v-if="run.failure_reason">{{ run.failure_reason }}</small>
          </div>
        </div>
      </section>

      <section class="panel settings-panel full-span">
        <div class="settings-heading"><Users :size="20" /><div><h3>{{ locale === 'zh-TW' ? '團隊與客服席位' : '团队与客服席位' }}</h3><span>{{ teams.length }} {{ locale === 'zh-TW' ? '個團隊' : '个团队' }} · {{ agents.length }}/{{ workspace?.max_agent_seats || 5 }} {{ t('seats') }}</span></div></div>
        <div class="seat-policy-note">
          <strong>{{ locale === 'zh-TW' ? '統一發放帳號' : '统一发放账号' }}</strong>
          <span>{{ locale === 'zh-TW' ? '目前工作區支援 5 個客服席位，不開放自行註冊。' : '当前工作区支持 5 个客服席位，不开放自行注册。' }}</span>
        </div>
        <div class="team-grid">
          <div v-for="team in teams" :key="team.id"><div class="team-mark">{{ team.name.slice(0, 1) }}</div><div><strong>{{ team.name }}</strong><span>{{ team.description }}</span></div><em v-if="team.is_default">{{ locale === 'zh-TW' ? '預設佇列' : '默认队列' }}</em></div>
          <div v-for="agent in agents" :key="`agent-${agent.id}`"><div class="agent-avatar">{{ agent.name.slice(0, 1) }}</div><div><strong>{{ agent.name }}</strong><span>{{ agent.email }}</span></div><em>{{ agent.role }}</em></div>
        </div>
      </section>

      <section class="panel settings-panel full-span">
        <div class="settings-heading settings-heading-actions">
          <MessageSquareText :size="20" />
          <div><h3>{{ locale === 'zh-TW' ? '快速回覆' : '快捷回复' }}</h3><span>{{ locale === 'zh-TW' ? '由管理員統一維護，客服可在會話輸入區使用' : '由管理员统一维护，客服可在会话输入区使用' }}</span></div>
          <button v-if="canManageReplies" class="primary-button compact-button" @click="openReplyEditor()"><Plus :size="15" />{{ locale === 'zh-TW' ? '新增' : '新增' }}</button>
        </div>
        <div class="quick-reply-settings-grid">
          <article v-for="item in quickReplies" :key="item.id">
            <header><strong>/{{ item.shortcut }}</strong><span>{{ item.language }}</span></header>
            <h4>{{ item.title }}</h4>
            <p>{{ item.body }}</p>
            <footer v-if="canManageReplies">
              <button class="icon-button compact" :title="locale === 'zh-TW' ? '編輯' : '编辑'" @click="openReplyEditor(item)"><Pencil :size="14" /></button>
              <button class="icon-button compact danger" :title="locale === 'zh-TW' ? '停用' : '停用'" @click="removeQuickReply(item)"><Trash2 :size="14" /></button>
            </footer>
          </article>
          <div v-if="!quickReplies.length" class="empty-state compact-state">{{ locale === 'zh-TW' ? '尚無快速回覆' : '暂无快捷回复' }}</div>
        </div>
      </section>
    </div>

    <div v-if="restModal" class="modal-backdrop" @click.self="restModal = false">
      <form class="modal-panel rest-action-modal" @submit.prevent="saveRestEndpoint">
        <div class="modal-heading"><div><p class="section-kicker">REST Action</p><h2>{{ editingRest ? (locale === 'zh-TW' ? '編輯連接器' : '编辑连接器') : (locale === 'zh-TW' ? '新增連接器' : '新增连接器') }}</h2></div><button type="button" class="icon-button" title="關閉" @click="restModal = false"><X :size="19" /></button></div>
        <div class="form-grid two-columns"><label><span>{{ locale === 'zh-TW' ? '名稱' : '名称' }}</span><input v-model="restForm.name" maxlength="120" required /></label><label><span>Timeout</span><input v-model.number="restForm.timeout_seconds" type="number" min="2" max="30" required /></label></div>
        <label><span>{{ locale === 'zh-TW' ? '說明' : '说明' }}</span><input v-model="restForm.description" maxlength="2000" /></label>
        <div class="form-grid two-columns"><label><span>HTTPS origin</span><input v-model="restForm.base_url" type="url" placeholder="https://api.example.com" required /></label><label><span>Path pattern</span><input v-model="restForm.path_pattern" placeholder="/v1/orders/*" required /></label></div>
        <fieldset class="rest-method-selector"><legend>HTTP methods</legend><button v-for="method in ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']" :key="method" type="button" :class="{ active: restForm.allowed_methods.includes(method) }" @click="toggleRestMethod(method)">{{ method }}</button></fieldset>
        <label class="toggle-row"><span>{{ locale === 'zh-TW' ? '每次調用要求有效身份核驗' : '每次调用要求有效身份核验' }}</span><input v-model="restForm.requires_identity_verification" type="checkbox" /></label>
        <div class="form-grid two-columns"><label><span>Credential header</span><input v-model="restForm.secret_header_name" autocomplete="off" placeholder="Authorization" /></label><label><span>{{ editingRest?.has_secret ? (locale === 'zh-TW' ? '替換密鑰（留空保留）' : '替换密钥（留空保留）') : (locale === 'zh-TW' ? 'API 密鑰' : 'API 密钥') }}</span><input v-model="restForm.secret_value" type="password" autocomplete="new-password" /></label></div>
        <label v-if="editingRest?.has_secret" class="toggle-row"><span>{{ locale === 'zh-TW' ? '移除現有密鑰' : '移除现有密钥' }}</span><input v-model="restForm.clear_secret" type="checkbox" /></label>
        <p v-if="restError" class="form-error">{{ restError }}</p>
        <div class="modal-actions"><button class="secondary-button" type="button" @click="restModal = false">{{ locale === 'zh-TW' ? '取消' : '取消' }}</button><button class="primary-button" type="submit" :disabled="savingRest"><ShieldCheck :size="16" />{{ savingRest ? (locale === 'zh-TW' ? '儲存中' : '保存中') : (locale === 'zh-TW' ? '儲存草稿' : '保存草稿') }}</button></div>
      </form>
    </div>

    <div v-if="replyModal" class="modal-backdrop" @click.self="replyModal = false">
      <form class="modal-panel small-modal" @submit.prevent="saveQuickReply">
        <div class="modal-heading">
          <div><p class="section-kicker">Quick reply</p><h2>{{ editingReply ? (locale === 'zh-TW' ? '編輯快速回覆' : '编辑快捷回复') : (locale === 'zh-TW' ? '新增快速回覆' : '新增快捷回复') }}</h2></div>
          <button type="button" class="icon-button" :title="locale === 'zh-TW' ? '關閉' : '关闭'" @click="replyModal = false"><X :size="19" /></button>
        </div>
        <div class="form-grid two-columns">
          <label><span>{{ locale === 'zh-TW' ? '快速鍵' : '快捷标识' }}</span><input v-model="replyForm.shortcut" required pattern="[a-zA-Z0-9._-]+" placeholder="refund-check" /></label>
          <label><span>{{ locale === 'zh-TW' ? '語言' : '语言' }}</span><select v-model="replyForm.language"><option value="zh-CN">簡體中文</option><option value="zh-TW">繁體中文</option></select></label>
        </div>
        <label><span>{{ locale === 'zh-TW' ? '標題' : '标题' }}</span><input v-model="replyForm.title" required /></label>
        <label><span>{{ locale === 'zh-TW' ? '回覆內容' : '回复内容' }}</span><textarea v-model="replyForm.body" rows="5" required /></label>
        <p v-if="replyError" class="form-error">{{ replyError }}</p>
        <div class="modal-actions">
          <button type="button" class="secondary-button" @click="replyModal = false">{{ locale === 'zh-TW' ? '取消' : '取消' }}</button>
          <button class="primary-button" type="submit" :disabled="savingReply">{{ savingReply ? (locale === 'zh-TW' ? '正在儲存' : '正在保存') : (locale === 'zh-TW' ? '儲存' : '保存') }}</button>
        </div>
      </form>
    </div>
  </div>
</template>
