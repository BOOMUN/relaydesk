<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Activity,
  ArrowLeft,
  Bell,
  Bot,
  BookOpenText,
  Check,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleUserRound,
  ClipboardList,
  Clock3,
  Filter,
  EyeOff,
  FileCheck2,
  Inbox,
  Languages,
  ListChecks,
  MessageCirclePlus,
  MessageSquareText,
  Plus,
  RefreshCw,
  RotateCcw,
  Rows3,
  Search,
  Send,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  StickyNote,
  Tag,
  UserRoundCheck,
  Users,
  X,
  Zap,
} from '@lucide/vue'
import { api } from '../api'
import { playHandoffChime } from '../handoff-audio'
import { useLocale } from '../i18n'
import type {
  ActionExecution,
  Agent,
  AutomationFormSession,
  Bootstrap,
  Contact,
  Conversation,
  ConversationActivity,
  InboxStats,
  IdentityVerification,
  Message,
  MessageTranslation,
  QuickReply,
  Team,
  WhatsAppTemplate,
} from '../types'

const props = defineProps<{ session: Bootstrap }>()
const { locale, t } = useLocale()

const conversations = ref<Conversation[]>([])
const selected = ref<Conversation | null>(null)
const teams = ref<Team[]>([])
const agents = ref<Agent[]>([])
const quickReplies = ref<QuickReply[]>([])
const activity = ref<ConversationActivity[]>([])
const conversationActions = ref<ActionExecution[]>([])
const automationSessions = ref<AutomationFormSession[]>([])
const identityVerifications = ref<IdentityVerification[]>([])
const whatsappTemplates = ref<WhatsAppTemplate[]>([])
const stats = ref<InboxStats>({ all: 0, open: 0, pending: 0, solved: 0, unread: 0, unassigned: 0, mine: 0 })
const total = ref(0)
const page = ref(1)
const pageSize = 30

const queueView = ref('all')
const statusFilter = ref('all')
const priorityFilter = ref('')
const teamFilter = ref('')
const agentFilter = ref('')
const sort = ref('newest')
const search = ref('')
const showFilters = ref(false)

const loading = ref(true)
const refreshing = ref(false)
const sending = ref(false)
const deliveryActionId = ref<number | null>(null)
const messageBody = ref('')
const composerMode = ref<'reply' | 'note'>('reply')
const showQuickReplies = ref(false)
const quickReplySearch = ref('')
const errorMessage = ref('')
const actionProcessingId = ref<string | null>(null)
const showIdentityVerification = ref(false)
const verifyingIdentity = ref(false)
const identityForm = ref({
  method: 'order_details' as 'order_details' | 'registered_phone' | 'email_otp' | 'sms_otp' | 'staff_review',
  evidence_reference: '',
  evidence_hint: '',
  expires_minutes: 30,
})
const translations = ref<Record<number, string>>({})
const translationErrors = ref<Record<number, string>>({})
const translatingMessageId = ref<number | null>(null)
const translationMenu = ref<{ message: Message; x: number; y: number } | null>(null)
const channelComposer = ref<'template' | 'buttons' | 'list' | null>(null)
const channelSending = ref(false)
const selectedTemplateId = ref<number | null>(null)
const templateParameters = ref<string[]>([])
const interactiveForm = ref({
  body: '',
  header: '',
  footer: '',
  buttonText: '查看選項',
  sectionTitle: '選項',
  buttons: [
    { id: 'option_1', title: '選項 1' },
    { id: 'option_2', title: '選項 2' },
  ],
  rows: [
    { id: 'row_1', title: '選項 1', description: '' },
    { id: 'row_2', title: '選項 2', description: '' },
  ],
})

const tagInput = ref('')
const customFields = ref<Array<{ key: string; value: string }>>([])
const savingContact = ref(false)
const profileDirty = ref(false)

const showSimulator = ref(false)
const simulating = ref(false)
const simulator = ref({ phone: '+8613900001000', display_name: '新客戶', body: '請問你們的退貨期限是多久？' })
const conversationList = ref<HTMLElement | null>(null)
const thread = ref<HTMLElement | null>(null)

let pollTimer: number | undefined
let searchTimer: number | undefined
let eventSource: EventSource | undefined
let syncPromise: Promise<void> | undefined
let syncQueued = false
let translationRequestSequence = 0
let handoffEventsInitialized = false
let activeHandoffIds = new Set<number>()
let revealListOnNextRefresh = false

const queueTabs = computed(() => [
  { id: 'mine', label: t('mine'), icon: CircleUserRound, count: stats.value.mine },
  { id: 'unassigned', label: t('unassigned'), icon: Users, count: stats.value.unassigned },
  { id: 'unread', label: t('unread'), icon: Bell, count: stats.value.unread },
  { id: 'all', label: t('all'), icon: Inbox, count: stats.value.all },
])

const statusTabs = computed(() => [
  { id: 'all', label: t('all'), count: stats.value.all },
  { id: 'open', label: t('open'), count: stats.value.open },
  { id: 'pending', label: t('pending'), count: stats.value.pending },
  { id: 'solved', label: t('solved'), count: stats.value.solved },
])

const statusLabel = computed<Record<string, string>>(() => ({
  open: t('open'),
  pending: t('pending'),
  expired: locale.value === 'zh-TW' ? '視窗過期' : '窗口过期',
  solved: t('solved'),
  blocked: locale.value === 'zh-TW' ? '已封鎖' : '已阻止',
}))

const priorityLabel = computed<Record<string, string>>(() => ({
  low: locale.value === 'zh-TW' ? '低' : '低',
  normal: locale.value === 'zh-TW' ? '一般' : '普通',
  high: locale.value === 'zh-TW' ? '高' : '高',
  urgent: locale.value === 'zh-TW' ? '緊急' : '紧急',
}))

const routeLabel = computed<Record<string, string>>(() => ({
  greeting: locale.value === 'zh-TW' ? '問候' : '问候',
  knowledge: locale.value === 'zh-TW' ? '知識庫' : '知识库',
  order: locale.value === 'zh-TW' ? '訂單工具' : '订单工具',
  handoff: locale.value === 'zh-TW' ? '人工接管' : '人工接管',
}))

const deliveryLabel = computed<Record<string, string>>(() => ({
  pending: locale.value === 'zh-TW' ? '傳送中' : '发送中',
  sent: locale.value === 'zh-TW' ? '已傳送' : '已发送',
  delivered: locale.value === 'zh-TW' ? '已送達' : '已送达',
  read: locale.value === 'zh-TW' ? '已讀' : '已读',
  played: locale.value === 'zh-TW' ? '已播放' : '已播放',
  failed: locale.value === 'zh-TW' ? '傳送失敗' : '发送失败',
  deleted: locale.value === 'zh-TW' ? '已刪除' : '已删除',
  unconfirmed: locale.value === 'zh-TW' ? '結果待確認' : '结果待确认',
}))

const selectedMessages = computed(() => selected.value?.messages || [])
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const pageDescription = computed(() => {
  if (!total.value) return '0'
  const start = (page.value - 1) * pageSize + 1
  const end = Math.min(page.value * pageSize, total.value)
  return `${start}-${end} / ${total.value}`
})
const filteredQuickReplies = computed(() => {
  const term = quickReplySearch.value.trim().toLowerCase()
  return quickReplies.value.filter((item) => {
    if (!term) return true
    return `${item.shortcut} ${item.title} ${item.body}`.toLowerCase().includes(term)
  })
})
const selectedTemplate = computed(() => (
  whatsappTemplates.value.find((item) => item.id === selectedTemplateId.value) || null
))
const visibleActions = computed(() => conversationActions.value
  .filter((item) => ['pending_confirmation', 'failed'].includes(item.status))
  .slice(0, 4))
const activeIdentityVerification = computed(() => identityVerifications.value.find((item) => (
  item.status === 'verified' && new Date(item.expires_at).getTime() > Date.now()
)) || null)
const latestAutomationSession = computed(() => (
  automationSessions.value.find((item) => ['active', 'paused'].includes(item.status))
  || automationSessions.value[0]
  || null
))
const automationFields = computed(() => latestAutomationSession.value?.definition_json.fields || [])
const automationProgress = computed(() => {
  const session = latestAutomationSession.value
  const total = automationFields.value.length
  if (!session || !total) return 0
  const answered = automationFields.value.filter((field) => (
    Boolean(field.key) && Object.hasOwn(session.answers_json, String(field.key))
  )).length
  return Math.round((answered / total) * 100)
})
const identitySectionVisible = computed(() => (
  identityVerifications.value.length > 0
  || conversationActions.value.some((item) => (
    item.status === 'pending_confirmation' && item.requires_identity_verification
  ))
))
const serviceWindowOpen = computed(() => Boolean(
  selected.value?.service_window_expires_at
  && new Date(selected.value.service_window_expires_at).getTime() > Date.now(),
))

const serviceWindow = computed(() => {
  if (!selected.value?.service_window_expires_at) return locale.value === 'zh-TW' ? '未知' : '未知'
  const remaining = new Date(selected.value.service_window_expires_at).getTime() - Date.now()
  if (remaining <= 0) return locale.value === 'zh-TW' ? '已過期' : '已过期'
  const hours = Math.floor(remaining / 3_600_000)
  const minutes = Math.floor((remaining % 3_600_000) / 60_000)
  return locale.value === 'zh-TW' ? `${hours} 小時 ${minutes} 分鐘` : `${hours} 小时 ${minutes} 分钟`
})

function dateLocale() {
  return locale.value === 'zh-TW' ? 'zh-TW' : 'zh-CN'
}

function formatListTime(value: string) {
  const date = new Date(value)
  const today = new Date()
  if (date.toDateString() === today.toDateString()) {
    return new Intl.DateTimeFormat(dateLocale(), { hour: '2-digit', minute: '2-digit' }).format(date)
  }
  return new Intl.DateTimeFormat(dateLocale(), { month: 'numeric', day: 'numeric' }).format(date)
}

function formatMessageTime(value: string) {
  return new Intl.DateTimeFormat(dateLocale(), { hour: '2-digit', minute: '2-digit' }).format(new Date(value))
}

function formatActivityTime(value: string) {
  return new Intl.DateTimeFormat(dateLocale(), { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
}

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat(dateLocale(), {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

function automationStatusLabel(status: AutomationFormSession['status']) {
  const labels: Record<AutomationFormSession['status'], string> = locale.value === 'zh-TW'
    ? { active: '收集中', paused: '已暫停', completed: '已完成', timed_out: '已超時', handed_off: '已轉人工' }
    : { active: '收集中', paused: '已暂停', completed: '已完成', timed_out: '已超时', handed_off: '已转人工' }
  return labels[status]
}

function automationWorkflowLabel(session: AutomationFormSession) {
  if (session.workflow_key === 'lead_qualification') {
    return locale.value === 'zh-TW' ? '線索資格' : '线索资格'
  }
  return locale.value === 'zh-TW' ? '訂單資料' : '订单资料'
}

function automationFieldPrompt(field: { key?: string; prompt?: string; prompt_en?: string | null }) {
  const fallback = String(field.key || (locale.value === 'zh-TW' ? '未命名欄位' : '未命名字段'))
  return String(field.prompt || field.prompt_en || fallback)
}

function automationFieldAnswered(field: { key?: string }) {
  return Boolean(
    latestAutomationSession.value
    && field.key
    && Object.hasOwn(latestAutomationSession.value.answers_json, field.key),
  )
}

function automationAnswer(field: { key?: string }) {
  if (!latestAutomationSession.value || !field.key) return ''
  const value = latestAutomationSession.value.answers_json[field.key]
  return typeof value === 'string' || typeof value === 'number'
    ? String(value)
    : JSON.stringify(value)
}

function messageHasEnglish(message: Message) {
  return message.content_type === 'text' && /[A-Za-z]/.test(message.body)
}

function closeTranslationMenu() {
  translationMenu.value = null
}

function openTranslationMenu(event: MouseEvent, message: Message) {
  const menuWidth = 220
  const menuHeight = translations.value[message.id] || translationErrors.value[message.id] ? 116 : 64
  translationMenu.value = {
    message,
    x: Math.max(8, Math.min(event.clientX, window.innerWidth - menuWidth - 8)),
    y: Math.max(8, Math.min(event.clientY, window.innerHeight - menuHeight - 8)),
  }
}

function clearTemporaryTranslations() {
  translationRequestSequence += 1
  translations.value = {}
  translationErrors.value = {}
  translatingMessageId.value = null
  closeTranslationMenu()
}

async function translateContextMessage() {
  const target = translationMenu.value?.message
  if (!selected.value || !target || !messageHasEnglish(target) || translatingMessageId.value !== null) return
  const conversationId = selected.value.id
  const requestSequence = ++translationRequestSequence
  closeTranslationMenu()
  translatingMessageId.value = target.id
  const nextErrors = { ...translationErrors.value }
  delete nextErrors[target.id]
  translationErrors.value = nextErrors
  try {
    const result = await api.post<MessageTranslation>(
      `/api/conversations/${conversationId}/messages/${target.id}/translate`,
    )
    if (translationRequestSequence === requestSequence && selected.value?.id === conversationId) {
      translations.value = { ...translations.value, [target.id]: result.translated_text }
    }
  } catch (error) {
    if (translationRequestSequence === requestSequence && selected.value?.id === conversationId) {
      translationErrors.value = {
        ...translationErrors.value,
        [target.id]: error instanceof Error ? error.message : '翻譯失敗，請稍後再試',
      }
    }
  } finally {
    if (translationRequestSequence === requestSequence) translatingMessageId.value = null
  }
}

function hideContextTranslation() {
  const messageId = translationMenu.value?.message.id
  if (messageId === undefined) return
  const nextTranslations = { ...translations.value }
  const nextErrors = { ...translationErrors.value }
  delete nextTranslations[messageId]
  delete nextErrors[messageId]
  translations.value = nextTranslations
  translationErrors.value = nextErrors
  closeTranslationMenu()
}

function handleTranslationMenuKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') closeTranslationMenu()
}

function activityText(item: ConversationActivity) {
  if (item.action === 'conversation.note_added') return t('activityNote')
  if (item.action === 'conversation.message_sent') return t('activitySent')
  return t('activityUpdated')
}

function prepareContactDraft(contact: Contact) {
  tagInput.value = ''
  customFields.value = Object.entries(contact.custom_attributes || {}).map(([key, value]) => ({
    key,
    value: typeof value === 'string' ? value : JSON.stringify(value),
  }))
  profileDirty.value = false
}

async function refreshStats() {
  stats.value = await api.get<InboxStats>('/api/inbox/stats')
}

async function refreshQuickReplies() {
  quickReplies.value = await api.get<QuickReply[]>('/api/quick-replies')
}

async function refreshList(selectFirst = false) {
  const params = new URLSearchParams({
    view: queueView.value,
    sort: sort.value,
    page: String(page.value),
    page_size: String(pageSize),
  })
  if (statusFilter.value !== 'all') params.set('status', statusFilter.value)
  if (priorityFilter.value) params.set('priority', priorityFilter.value)
  if (teamFilter.value) params.set('team_id', teamFilter.value)
  if (agentFilter.value) params.set('assigned_user_id', agentFilter.value)
  if (search.value.trim()) params.set('search', search.value.trim())
  const result = await api.getPage<Conversation[]>(`/api/conversations?${params}`)
  conversations.value = result.data
  total.value = result.total
  if (selectFirst && !selected.value && result.data.length) await selectConversation(result.data[0].id)
}

async function loadActivity(id: number) {
  activity.value = await api.get<ConversationActivity[]>(`/api/conversations/${id}/activity`)
}

async function loadConversationActions(id: number) {
  conversationActions.value = await api.get<ActionExecution[]>(
    `/api/actions?conversation_id=${id}&limit=50`,
  )
}

async function loadAutomationContext(id: number) {
  const [sessions, verifications] = await Promise.all([
    api.get<AutomationFormSession[]>(`/api/automation/sessions?conversation_id=${id}&limit=20`),
    api.get<IdentityVerification[]>(
      `/api/automation/conversations/${id}/identity-verifications`,
    ),
  ])
  automationSessions.value = sessions
  identityVerifications.value = verifications
}

async function loadWhatsAppTemplates() {
  whatsappTemplates.value = await api.get<WhatsAppTemplate[]>(
    '/api/whatsapp/templates?status=APPROVED',
  )
}

async function selectConversation(id: number) {
  if (profileDirty.value && selected.value && selected.value.id !== id) {
    const prompt = locale.value === 'zh-TW' ? '客戶資料尚未儲存，仍要切換會話嗎？' : '客户资料尚未保存，仍要切换会话吗？'
    if (!window.confirm(prompt)) return
  }
  if (selected.value?.id !== id) {
    clearTemporaryTranslations()
    automationSessions.value = []
    identityVerifications.value = []
  }
  errorMessage.value = ''
  selected.value = await api.patch<Conversation>(`/api/conversations/${id}`, { mark_read: true })
  prepareContactDraft(selected.value.contact)
  const index = conversations.value.findIndex((item) => item.id === id)
  if (index >= 0) conversations.value[index] = { ...conversations.value[index], unread_count: 0 }
  await Promise.all([
    loadActivity(id),
    loadConversationActions(id),
    loadAutomationContext(id),
    refreshStats(),
  ])
  await scrollToBottom()
}

async function refreshSelected() {
  if (!selected.value) return
  const id = selected.value.id
  const localContact = selected.value.contact
  const detail = await api.get<Conversation>(`/api/conversations/${id}`)
  if (profileDirty.value) detail.contact = localContact
  selected.value = detail
  if (!profileDirty.value) prepareContactDraft(detail.contact)
  await Promise.all([loadActivity(id), loadConversationActions(id), loadAutomationContext(id)])
}

function syncFromServer() {
  syncQueued = true
  if (syncPromise) return syncPromise
  syncPromise = (async () => {
    refreshing.value = true
    try {
      while (syncQueued) {
        syncQueued = false
        await Promise.all([refreshList(), refreshStats(), refreshSelected(), refreshQuickReplies()])
      }
    } finally {
      refreshing.value = false
      syncPromise = undefined
    }
  })()
  return syncPromise
}

async function scrollToBottom() {
  await nextTick()
  if (thread.value) thread.value.scrollTop = thread.value.scrollHeight
}

function isPinnedHandoff(conversation: Conversation) {
  return conversation.status === 'pending'
    && conversation.ai_route === 'handoff'
    && !conversation.ai_enabled
}

async function applyLocalMessageActivity(
  conversationId: number,
  message: Pick<Message, 'body' | 'sender_type' | 'created_at'>,
) {
  const index = conversations.value.findIndex((item) => item.id === conversationId)
  if (index < 0) return
  const updated = {
    ...conversations.value[index],
    last_message: message.body,
    last_message_sender: message.sender_type,
    last_message_at: message.created_at,
    assigned_user_id: props.session.user.id,
    assigned_user: props.session.user.name,
    unread_count: 0,
  }
  if (sort.value !== 'newest' || page.value !== 1) {
    conversations.value[index] = updated
    return
  }
  const remaining = conversations.value.filter((item) => item.id !== updated.id)
  const insertAt = isPinnedHandoff(updated)
    ? 0
    : remaining.findIndex((item) => !isPinnedHandoff(item))
  remaining.splice(insertAt < 0 ? remaining.length : insertAt, 0, updated)
  conversations.value = remaining
  await nextTick()
  if (conversationList.value) conversationList.value.scrollTop = 0
}

async function updateConversation(patch: Record<string, unknown>) {
  if (!selected.value) return
  errorMessage.value = ''
  try {
    const localContact = selected.value.contact
    const updated = await api.patch<Conversation>(`/api/conversations/${selected.value.id}`, patch)
    if (profileDirty.value) updated.contact = localContact
    selected.value = updated
    await Promise.all([refreshList(), refreshStats(), loadActivity(selected.value.id)])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '更新失敗'
  }
}

async function sendMessage() {
  if (!selected.value || !messageBody.value.trim() || sending.value) return
  const conversationId = selected.value.id
  const outboundBody = messageBody.value.trim()
  const isInternal = composerMode.value === 'note'
  sending.value = true
  errorMessage.value = ''
  if (!isInternal) {
    await applyLocalMessageActivity(conversationId, {
      body: outboundBody,
      sender_type: 'agent',
      created_at: new Date().toISOString(),
    })
  }
  try {
    const message = await api.post<Message>(`/api/conversations/${conversationId}/messages`, {
      body: outboundBody,
      internal: isInternal,
    })
    if (selected.value?.id === conversationId) {
      selected.value.messages = [...(selected.value.messages || []), message]
    }
    if (!isInternal) {
      if (selected.value?.id === conversationId) {
        selected.value.last_message = message.body
        selected.value.last_message_at = message.created_at
        selected.value.last_message_sender = message.sender_type
      }
      await applyLocalMessageActivity(conversationId, message)
    }
    if (selected.value?.id === conversationId) {
      selected.value.assigned_user_id = props.session.user.id
      selected.value.assigned_user = props.session.user.name
    }
    messageBody.value = ''
    showQuickReplies.value = false
    await Promise.all([
      refreshList(),
      refreshStats(),
      selected.value ? loadActivity(selected.value.id) : Promise.resolve(),
    ])
    if (selected.value?.id === conversationId) await scrollToBottom()
  } catch (error) {
    if (!isInternal) await refreshList()
    errorMessage.value = error instanceof Error ? error.message : '傳送失敗'
  } finally {
    sending.value = false
  }
}

function replaceSelectedMessage(updated: Message) {
  if (!selected.value?.messages) return
  selected.value.messages = selected.value.messages.map((item) => (
    item.id === updated.id ? updated : item
  ))
}

async function reconcileDelivery(message: Message) {
  if (!selected.value || deliveryActionId.value !== null) return
  deliveryActionId.value = message.id
  errorMessage.value = ''
  try {
    const updated = await api.post<Message>(
      `/api/conversations/${selected.value.id}/messages/${message.id}/delivery/reconcile`,
    )
    replaceSelectedMessage(updated)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : (
      locale.value === 'zh-TW' ? '無法更新傳送狀態' : '无法更新发送状态'
    )
  } finally {
    deliveryActionId.value = null
  }
}

async function retryDelivery(message: Message) {
  if (!selected.value || deliveryActionId.value !== null) return
  const prompt = locale.value === 'zh-TW'
    ? '確定重新傳送這則失敗訊息嗎？系統只允許重試明確失敗的訊息。'
    : '确定重新发送这条失败消息吗？系统只允许重试明确失败的消息。'
  if (!window.confirm(prompt)) return
  deliveryActionId.value = message.id
  errorMessage.value = ''
  try {
    const updated = await api.post<Message>(
      `/api/conversations/${selected.value.id}/messages/${message.id}/retry`,
    )
    replaceSelectedMessage(updated)
    await loadActivity(selected.value.id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : (
      locale.value === 'zh-TW' ? '重試失敗' : '重试失败'
    )
  } finally {
    deliveryActionId.value = null
  }
}

function actionTitle(item: ActionExecution) {
  const labels: Record<string, string> = {
    'conversation.handoff': locale.value === 'zh-TW' ? '轉交人工客服' : '转交人工客服',
    'conversation.resume_ai': locale.value === 'zh-TW' ? '恢復 AI' : '恢复 AI',
    'contact.update_profile': locale.value === 'zh-TW' ? '更新聯絡人' : '更新联系人',
    'whatsapp.template.send': locale.value === 'zh-TW' ? '傳送 WhatsApp 範本' : '发送 WhatsApp 模板',
    'whatsapp.interactive.send': locale.value === 'zh-TW' ? '傳送互動訊息' : '发送互动消息',
    'order.sensitive.request': locale.value === 'zh-TW' ? '敏感訂單操作' : '敏感订单操作',
    'rest.api.call': 'REST API Action',
  }
  return labels[item.action_name] || item.action_name
}

function canConfirmAction(item: ActionExecution) {
  return !item.requires_identity_verification
    || item.identity_verified
    || activeIdentityVerification.value !== null
}

function openIdentityVerificationModal() {
  identityForm.value = {
    method: 'order_details',
    evidence_reference: '',
    evidence_hint: '',
    expires_minutes: 30,
  }
  errorMessage.value = ''
  showIdentityVerification.value = true
}

function closeIdentityVerificationModal() {
  identityForm.value.evidence_reference = ''
  showIdentityVerification.value = false
}

async function verifyIdentity() {
  if (!selected.value || verifyingIdentity.value || !identityForm.value.evidence_reference.trim()) return
  const conversationId = selected.value.id
  verifyingIdentity.value = true
  errorMessage.value = ''
  try {
    await api.post<IdentityVerification>(
      `/api/automation/conversations/${conversationId}/identity-verifications`,
      {
        ...identityForm.value,
        evidence_reference: identityForm.value.evidence_reference.trim(),
        evidence_hint: identityForm.value.evidence_hint.trim(),
      },
    )
    if (selected.value?.id === conversationId) {
      await Promise.all([loadAutomationContext(conversationId), loadConversationActions(conversationId)])
    }
    closeIdentityVerificationModal()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : (
      locale.value === 'zh-TW' ? '身份核驗失敗' : '身份核验失败'
    )
  } finally {
    identityForm.value.evidence_reference = ''
    verifyingIdentity.value = false
  }
}

async function confirmConversationAction(item: ActionExecution) {
  if (!selected.value || actionProcessingId.value !== null) return
  if (!canConfirmAction(item)) {
    openIdentityVerificationModal()
    return
  }
  const prompt = item.requires_identity_verification
    ? (locale.value === 'zh-TW'
      ? '身份已核驗。確定批准此敏感操作交由人工執行嗎？系統不會自動修改訂單。'
      : '身份已核验。确定批准此敏感操作交由人工执行吗？系统不会自动修改订单。')
    : (locale.value === 'zh-TW' ? '確定批准執行此 Action 嗎？' : '确定批准执行此 Action 吗？')
  if (!window.confirm(prompt)) return
  actionProcessingId.value = item.id
  errorMessage.value = ''
  try {
    await api.post<ActionExecution>(`/api/actions/${item.id}/confirm`)
    await Promise.all([refreshSelected(), refreshList(), refreshStats()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'Action 執行失敗'
  } finally {
    actionProcessingId.value = null
  }
}

async function rejectConversationAction(item: ActionExecution) {
  if (!selected.value) return
  const reason = window.prompt(locale.value === 'zh-TW' ? '請輸入拒絕原因' : '请输入拒绝原因')
  if (!reason?.trim()) return
  try {
    await api.post<ActionExecution>(`/api/actions/${item.id}/reject`, { reason: reason.trim() })
    await loadConversationActions(selected.value.id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'Action 拒絕失敗'
  }
}

function openChannelComposer(mode: 'template' | 'buttons' | 'list') {
  channelComposer.value = mode
  errorMessage.value = ''
  if (mode === 'template') {
    selectedTemplateId.value ||= whatsappTemplates.value[0]?.id || null
  }
}

function addInteractiveItem() {
  if (channelComposer.value === 'buttons' && interactiveForm.value.buttons.length < 3) {
    const index = interactiveForm.value.buttons.length + 1
    interactiveForm.value.buttons.push({ id: `option_${index}`, title: `選項 ${index}` })
  }
  if (channelComposer.value === 'list' && interactiveForm.value.rows.length < 10) {
    const index = interactiveForm.value.rows.length + 1
    interactiveForm.value.rows.push({ id: `row_${index}`, title: `選項 ${index}`, description: '' })
  }
}

async function sendChannelMessage() {
  if (!selected.value || !channelComposer.value || channelSending.value) return
  const conversationId = selected.value.id
  channelSending.value = true
  errorMessage.value = ''
  try {
    let message: Message
    if (channelComposer.value === 'template') {
      if (!selectedTemplateId.value) throw new Error('請選擇已批准的 WhatsApp 範本')
      const components = templateParameters.value.length
        ? [{ type: 'body', parameters: templateParameters.value.map((text) => ({ type: 'text', text })) }]
        : []
      message = await api.post<Message>(
        `/api/conversations/${conversationId}/whatsapp/template`,
        { template_id: selectedTemplateId.value, components },
      )
    } else {
      if (!interactiveForm.value.body.trim()) throw new Error('請輸入訊息內容')
      const common = {
        kind: channelComposer.value,
        body: interactiveForm.value.body.trim(),
        header: interactiveForm.value.header.trim() || null,
        footer: interactiveForm.value.footer.trim() || null,
      }
      const payload = channelComposer.value === 'buttons'
        ? {
            ...common,
            buttons: interactiveForm.value.buttons
              .filter((item) => item.id.trim() && item.title.trim())
              .map((item) => ({ id: item.id.trim(), title: item.title.trim() })),
          }
        : {
            ...common,
            button_text: interactiveForm.value.buttonText.trim(),
            sections: [{
              title: interactiveForm.value.sectionTitle.trim(),
              rows: interactiveForm.value.rows
                .filter((item) => item.id.trim() && item.title.trim())
                .map((item) => ({
                  id: item.id.trim(),
                  title: item.title.trim(),
                  description: item.description.trim() || null,
                })),
            }],
          }
      message = await api.post<Message>(
        `/api/conversations/${conversationId}/whatsapp/interactive`,
        payload,
      )
    }
    if (selected.value?.id === conversationId) {
      selected.value.messages = [...(selected.value.messages || []), message]
      selected.value.last_message = message.body
      selected.value.last_message_at = message.created_at
      selected.value.last_message_sender = message.sender_type
    }
    await applyLocalMessageActivity(conversationId, message)
    channelComposer.value = null
    await Promise.all([refreshList(), refreshStats(), loadConversationActions(conversationId)])
    if (selected.value?.id === conversationId) await scrollToBottom()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'WhatsApp 訊息傳送失敗'
  } finally {
    channelSending.value = false
  }
}

function insertQuickReply(item: QuickReply) {
  messageBody.value = item.body
  composerMode.value = 'reply'
  showQuickReplies.value = false
}

async function saveContactProfile() {
  if (!selected.value || savingContact.value) return
  savingContact.value = true
  errorMessage.value = ''
  try {
    const attributes: Record<string, string> = {}
    for (const field of customFields.value) {
      const key = field.key.trim()
      if (key) attributes[key] = field.value.trim()
    }
    const contact = await api.patch<Contact>(`/api/contacts/${selected.value.contact.id}`, {
      display_name: selected.value.contact.display_name,
      language: selected.value.contact.language,
      tags: selected.value.contact.tags,
      custom_attributes: attributes,
    })
    selected.value.contact = contact
    prepareContactDraft(contact)
    await refreshList()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '儲存失敗'
  } finally {
    savingContact.value = false
  }
}

function addTag() {
  if (!selected.value) return
  const value = tagInput.value.trim()
  if (value && !selected.value.contact.tags.includes(value)) {
    selected.value.contact.tags = [...selected.value.contact.tags, value]
    profileDirty.value = true
  }
  tagInput.value = ''
}

function removeTag(value: string) {
  if (!selected.value) return
  selected.value.contact.tags = selected.value.contact.tags.filter((item) => item !== value)
  profileDirty.value = true
}

function addCustomField() {
  customFields.value.push({ key: '', value: '' })
  profileDirty.value = true
}

function changePage(nextPage: number) {
  if (nextPage < 1 || nextPage > totalPages.value) return
  page.value = nextPage
  void refreshList()
}

async function simulateInbound() {
  if (simulating.value) return
  simulating.value = true
  try {
    const response = await api.post<{ conversation: Conversation }>('/api/demo/inbound', simulator.value)
    clearTemporaryTranslations()
    selected.value = response.conversation
    prepareContactDraft(response.conversation.contact)
    showSimulator.value = false
    await Promise.all([refreshList(), refreshStats(), loadActivity(response.conversation.id)])
    await scrollToBottom()
  } finally {
    simulating.value = false
  }
}

function scheduleEventRefresh(revealList = false) {
  revealListOnNextRefresh ||= revealList
  void syncFromServer().then(async () => {
    if (!revealListOnNextRefresh) return
    revealListOnNextRefresh = false
    await nextTick()
    if (conversationList.value) conversationList.value.scrollTop = 0
  })
}

function handleInboxEvent(event: MessageEvent<string>) {
  try {
    const payload = JSON.parse(event.data) as {
      type?: string
      activity?: string
      conversation_id?: unknown
      sender_type?: unknown
      handoff_ids?: unknown
    }
    if (payload.type === 'inbox.updated' && Array.isArray(payload.handoff_ids)) {
      const nextHandoffIds = new Set(
        payload.handoff_ids.filter((value): value is number => Number.isInteger(value)),
      )
      const hasNewHandoff = [...nextHandoffIds].some((id) => !activeHandoffIds.has(id))
      if (handoffEventsInitialized && hasNewHandoff) {
        page.value = 1
        revealListOnNextRefresh = true
        void playHandoffChime()
      }
      activeHandoffIds = nextHandoffIds
      handoffEventsInitialized = true
    }
    if (payload.type === 'inbox.updated' && payload.activity === 'message') {
      page.value = 1
      revealListOnNextRefresh = true
    }
    if (payload.type === 'inbox.updated' && payload.activity === 'initial') return
  } catch {
    // A malformed optional event payload must not stop normal inbox refreshes.
  }
  scheduleEventRefresh(revealListOnNextRefresh)
}

watch([queueView, statusFilter, priorityFilter, teamFilter, agentFilter, sort], () => {
  page.value = 1
  void refreshList()
})

watch(selectedTemplateId, () => {
  const serialized = JSON.stringify(selectedTemplate.value?.components || [])
  const indexes = [...serialized.matchAll(/\{\{(\d+)\}\}/g)].map((match) => Number(match[1]))
  const count = indexes.length ? Math.max(...indexes) : 0
  templateParameters.value = Array.from(
    { length: count },
    (_, index) => templateParameters.value[index] || '',
  )
})

watch(search, () => {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => {
    page.value = 1
    void refreshList()
  }, 250)
})

onMounted(async () => {
  document.addEventListener('click', closeTranslationMenu)
  document.addEventListener('keydown', handleTranslationMenuKeydown)
  window.addEventListener('blur', closeTranslationMenu)
  try {
    const [teamRows, agentRows, replyRows] = await Promise.all([
      api.get<Team[]>('/api/teams'),
      api.get<Agent[]>('/api/agents'),
      api.get<QuickReply[]>('/api/quick-replies'),
      refreshStats(),
      loadWhatsAppTemplates(),
    ])
    teams.value = teamRows
    agents.value = agentRows
    quickReplies.value = replyRows
    await refreshList(true)
    eventSource = new EventSource('/api/events')
    eventSource.onmessage = handleInboxEvent
    pollTimer = window.setInterval(() => void syncFromServer(), 30_000)
  } finally {
    loading.value = false
  }
})

onBeforeUnmount(() => {
  eventSource?.close()
  window.clearInterval(pollTimer)
  window.clearTimeout(searchTimer)
  document.removeEventListener('click', closeTranslationMenu)
  document.removeEventListener('keydown', handleTranslationMenuKeydown)
  window.removeEventListener('blur', closeTranslationMenu)
})
</script>

<template>
  <div class="inbox-layout inbox-v2" :class="{ 'has-selection': selected }">
    <section class="conversation-column">
      <div class="queue-tabs" aria-label="Inbox views">
        <button
          v-for="tab in queueTabs"
          :key="tab.id"
          :class="{ active: queueView === tab.id }"
          @click="queueView = tab.id"
        >
          <component :is="tab.icon" :size="15" />
          <span>{{ tab.label }}</span>
          <em>{{ tab.count }}</em>
        </button>
      </div>

      <div class="conversation-toolbar">
        <div class="search-box">
          <Search :size="16" />
          <input v-model="search" :placeholder="t('search')" />
        </div>
        <button class="icon-button" :class="{ active: showFilters }" :title="t('status')" @click="showFilters = !showFilters">
          <Filter :size="17" />
        </button>
        <button v-if="session.integration.mode === 'demo'" class="icon-button accent" title="模擬客戶訊息" @click="showSimulator = true">
          <MessageCirclePlus :size="18" />
        </button>
      </div>

      <div class="status-tabs inbox-status-tabs">
        <button v-for="tab in statusTabs" :key="tab.id" :class="{ active: statusFilter === tab.id }" @click="statusFilter = tab.id">
          {{ tab.label }} <span>{{ tab.count }}</span>
        </button>
      </div>

      <div v-if="showFilters" class="inbox-filters">
        <select v-model="priorityFilter" :aria-label="t('priority')">
          <option value="">{{ locale === 'zh-TW' ? '所有優先級' : '所有优先级' }}</option>
          <option v-for="(label, value) in priorityLabel" :key="value" :value="value">{{ label }}</option>
        </select>
        <select v-model="teamFilter" :aria-label="t('team')">
          <option value="">{{ locale === 'zh-TW' ? '所有團隊' : '所有团队' }}</option>
          <option v-for="team in teams" :key="team.id" :value="String(team.id)">{{ team.name }}</option>
        </select>
        <select v-model="agentFilter" :aria-label="t('agent')">
          <option value="">{{ locale === 'zh-TW' ? '所有客服' : '所有客服' }}</option>
          <option v-for="agent in agents" :key="agent.id" :value="String(agent.id)">{{ agent.name }}</option>
        </select>
        <select v-model="sort" aria-label="Sort">
          <option value="newest">{{ locale === 'zh-TW' ? '最新活動' : '最新活动' }}</option>
          <option value="oldest">{{ locale === 'zh-TW' ? '等待最久' : '等待最久' }}</option>
          <option value="priority">{{ locale === 'zh-TW' ? '優先級' : '优先级' }}</option>
        </select>
      </div>

      <div ref="conversationList" class="conversation-list">
        <div v-if="loading" class="loading-state compact-state">{{ locale === 'zh-TW' ? '正在載入會話' : '正在加载会话' }}</div>
        <button
          v-for="conversation in conversations"
          v-else
          :key="conversation.id"
          class="conversation-item"
          :class="{ selected: selected?.id === conversation.id, unread: conversation.unread_count > 0 }"
          @click="selectConversation(conversation.id)"
        >
          <div class="contact-avatar" :class="{ urgent: conversation.priority === 'urgent' }">
            {{ conversation.contact.display_name.slice(0, 1).toUpperCase() }}
            <span v-if="conversation.ai_enabled" class="avatar-ai"><Sparkles :size="9" /></span>
          </div>
          <div class="conversation-preview">
            <div class="conversation-name-row">
              <strong>{{ conversation.contact.display_name }}</strong>
              <time>{{ formatListTime(conversation.last_message_at) }}</time>
            </div>
            <p>{{ conversation.last_message }}</p>
            <div class="conversation-meta-row">
              <span :class="['mini-status', conversation.status]">{{ statusLabel[conversation.status] }}</span>
              <span v-if="conversation.assigned_user" class="assignee-label">{{ conversation.assigned_user }}</span>
              <span v-else class="assignee-label unassigned">{{ t('unassignedValue') }}</span>
              <span v-if="conversation.unread_count" class="unread-count">{{ conversation.unread_count }}</span>
            </div>
          </div>
        </button>
        <div v-if="!loading && !conversations.length" class="empty-state compact-state">
          <Inbox :size="25" />
          <span>{{ t('noConversations') }}</span>
        </div>
      </div>

      <footer class="conversation-pagination">
        <span>{{ pageDescription }}</span>
        <div>
          <button class="icon-button compact" :disabled="page <= 1" @click="changePage(page - 1)"><ChevronLeft :size="15" /></button>
          <button class="icon-button compact" :disabled="page >= totalPages" @click="changePage(page + 1)"><ChevronRight :size="15" /></button>
        </div>
      </footer>
    </section>

    <section v-if="selected" class="chat-column">
      <header class="chat-header">
        <div class="chat-contact-heading">
          <button class="icon-button mobile-back" title="返回會話列表" @click="selected = null"><ArrowLeft :size="19" /></button>
          <div class="contact-avatar">{{ selected.contact.display_name.slice(0, 1).toUpperCase() }}</div>
          <div>
            <h2>{{ selected.contact.display_name }}</h2>
            <span>{{ selected.contact.phone }} · {{ selected.assigned_team || t('unassignedValue') }}</span>
          </div>
        </div>
        <div class="chat-header-actions">
          <button v-if="!selected.assigned_user_id" class="claim-button" @click="updateConversation({ assigned_user_id: session.user.id, ai_enabled: false })">
            <UserRoundCheck :size="15" />{{ locale === 'zh-TW' ? '接手' : '接手' }}
          </button>
          <span :class="['status-badge', selected.status]">{{ statusLabel[selected.status] }}</span>
        </div>
      </header>

      <div ref="thread" class="message-thread" @scroll.passive="closeTranslationMenu">
        <div class="thread-date"><span>{{ locale === 'zh-TW' ? '目前會話' : '当前会话' }}</span></div>
        <template v-for="message in selectedMessages" :key="message.id">
          <article
            :class="['message-row', message.sender_type, { 'internal-message': message.direction === 'internal' }]"
            :aria-label="message.sender_type === 'ai' ? 'AI回答' : undefined"
          >
            <div v-if="message.sender_type !== 'customer'" class="message-sender-icon">
              <StickyNote v-if="message.direction === 'internal'" :size="15" />
              <Bot v-else-if="message.sender_type === 'ai'" :size="15" />
              <CircleUserRound v-else :size="15" />
            </div>
            <div class="message-content">
              <div class="message-byline">
                <strong>{{ message.sender_name || (message.sender_type === 'customer' ? selected.contact.display_name : t('agent')) }}</strong>
                <span v-if="message.sender_type === 'ai'" class="ai-label">AI</span>
                <span v-if="message.direction === 'internal'" class="note-label">{{ t('internalNote') }}</span>
                <time>{{ formatMessageTime(message.created_at) }}</time>
                <span
                  v-if="message.direction === 'outbound'"
                  class="delivery-state"
                  :class="`delivery-${message.delivery_status}`"
                  :title="deliveryLabel[message.delivery_status] || message.delivery_status"
                >
                  <Clock3 v-if="message.delivery_status === 'pending'" :size="13" />
                  <CircleAlert v-else-if="message.delivery_status === 'failed'" :size="13" />
                  <CheckCheck
                    v-else-if="['delivered', 'read', 'played'].includes(message.delivery_status)"
                    :size="14"
                  />
                  <Check v-else :size="13" />
                  {{ deliveryLabel[message.delivery_status] || message.delivery_status }}
                </span>
                <button
                  v-if="message.direction === 'outbound' && message.delivery_status === 'failed'"
                  class="delivery-action retry-action"
                  type="button"
                  :disabled="deliveryActionId !== null"
                  :title="locale === 'zh-TW' ? '重新傳送失敗訊息' : '重新发送失败消息'"
                  @click="retryDelivery(message)"
                >
                  <RotateCcw :size="12" :class="{ spin: deliveryActionId === message.id }" />
                  {{ locale === 'zh-TW' ? '重試' : '重试' }}
                </button>
                <button
                  v-else-if="
                    message.direction === 'outbound'
                      && session.integration.whatsapp_provider === 'evolution'
                      && message.external_id
                      && ['pending', 'sent'].includes(message.delivery_status)
                  "
                  class="delivery-action"
                  type="button"
                  :disabled="deliveryActionId !== null"
                  :title="locale === 'zh-TW' ? '向 WhatsApp 查詢最新狀態' : '向 WhatsApp 查询最新状态'"
                  @click="reconcileDelivery(message)"
                >
                  <RefreshCw :size="12" :class="{ spin: deliveryActionId === message.id }" />
                  {{ locale === 'zh-TW' ? '更新' : '更新' }}
                </button>
              </div>
              <div class="message-bubble" @contextmenu.prevent.stop="openTranslationMenu($event, message)">
                <p>{{ message.body }}</p>
                <div v-if="message.metadata_json.sources?.length" class="source-row">
                  <a
                    v-for="(source, sourceIndex) in message.metadata_json.sources"
                    :key="`${source.document_id || source.product_id || sourceIndex}-${sourceIndex}`"
                    class="source-citation"
                    :href="source.source_url || source.source"
                    target="_blank"
                    rel="noreferrer"
                    :title="[source.section_path, source.source_updated_at].filter(Boolean).join(' · ')"
                  >
                    <BookOpenText :size="12" />
                    <span>[{{ sourceIndex + 1 }}] {{ source.title }}</span>
                    <small v-if="source.section_path">{{ source.section_path }}</small>
                  </a>
                </div>
                <div v-if="message.metadata_json.action_failure_reason" class="message-action-error">
                  <CircleAlert :size="13" />
                  <span>{{ message.metadata_json.action_failure_reason }}</span>
                  <code v-if="message.metadata_json.action_error_code">{{ message.metadata_json.action_error_code }}</code>
                </div>
              </div>
              <div
                v-if="translatingMessageId === message.id || translations[message.id] || translationErrors[message.id]"
                :class="['message-translation', { error: translationErrors[message.id] }]"
              >
                <div class="translation-heading">
                  <Languages :size="13" />
                  <strong>繁體翻譯</strong>
                  <span>僅目前頁面顯示</span>
                </div>
                <p v-if="translatingMessageId === message.id" class="translation-loading"><RefreshCw :size="13" class="spin" />正在翻譯…</p>
                <p v-else-if="translationErrors[message.id]">{{ translationErrors[message.id] }}</p>
                <p v-else>{{ translations[message.id] }}</p>
              </div>
            </div>
          </article>
        </template>
      </div>

      <div v-if="!selected.ai_enabled" class="handoff-banner">
        <UserRoundCheck :size="17" />
        <span>{{ locale === 'zh-TW' ? 'AI 已暫停，目前由人工客服處理' : 'AI 已暂停，当前由人工客服处理' }}</span>
        <button @click="updateConversation({ ai_enabled: true })">{{ t('restoreAi') }}</button>
      </div>

      <div v-if="visibleActions.length" class="conversation-action-panel">
        <article v-for="item in visibleActions" :key="item.id" :class="item.status">
          <div class="action-state-icon"><CircleAlert v-if="item.status === 'failed'" :size="16" /><ShieldAlert v-else :size="16" /></div>
          <div><strong>{{ actionTitle(item) }}</strong><span>{{ item.failure_reason || item.confirmation_reason || item.purpose }}</span><small>{{ item.error_code || `${item.requested_by_type} · ${item.risk_level}` }}</small></div>
          <div v-if="item.status === 'pending_confirmation'" class="action-confirm-controls">
            <button
              v-if="item.requires_identity_verification && !canConfirmAction(item)"
              class="icon-button compact verify-action"
              :title="locale === 'zh-TW' ? '先核驗客戶身份' : '先核验客户身份'"
              @click="openIdentityVerificationModal"
            ><ShieldCheck :size="14" /></button>
            <button
              class="icon-button compact"
              :disabled="actionProcessingId !== null || !canConfirmAction(item)"
              :title="!canConfirmAction(item) ? (locale === 'zh-TW' ? '身份核驗後才可批准' : '身份核验后才可批准') : (locale === 'zh-TW' ? '批准執行' : '批准执行')"
              @click="confirmConversationAction(item)"
            ><Check :size="14" /></button>
            <button class="icon-button compact danger" :disabled="actionProcessingId !== null" :title="locale === 'zh-TW' ? '拒絕' : '拒绝'" @click="rejectConversationAction(item)"><X :size="14" /></button>
          </div>
        </article>
      </div>

      <section class="composer-shell" :class="{ 'note-mode': composerMode === 'note' }">
        <div class="composer-tabs">
          <button :class="{ active: composerMode === 'reply' }" @click="composerMode = 'reply'">
            <MessageSquareText :size="15" />{{ t('reply') }}
          </button>
          <button :class="{ active: composerMode === 'note' }" @click="composerMode = 'note'">
            <StickyNote :size="15" />{{ t('internalNote') }}
          </button>
          <span v-if="composerMode === 'note'">{{ locale === 'zh-TW' ? '不會傳送給客戶' : '不会发送给客户' }}</span>
        </div>
        <form class="composer" @submit.prevent="sendMessage">
          <textarea
            v-model="messageBody"
            rows="2"
            :disabled="selected.status === 'blocked' && composerMode === 'reply'"
            :placeholder="composerMode === 'note' ? t('notePlaceholder') : t('replyPlaceholder')"
            @keydown.ctrl.enter.prevent="sendMessage"
          />
            <div class="composer-actions">
              <div v-if="composerMode === 'reply'" class="channel-message-tools">
                <button type="button" class="composer-tool icon-only" :disabled="!whatsappTemplates.length" :title="locale === 'zh-TW' ? '傳送已批准範本' : '发送已批准模板'" @click="openChannelComposer('template')"><FileCheck2 :size="16" /></button>
                <button type="button" class="composer-tool icon-only" :disabled="!serviceWindowOpen" :title="locale === 'zh-TW' ? '傳送回覆按鈕' : '发送回复按钮'" @click="openChannelComposer('buttons')"><ListChecks :size="16" /></button>
                <button type="button" class="composer-tool icon-only" :disabled="!serviceWindowOpen" :title="locale === 'zh-TW' ? '傳送選項列表' : '发送选项列表'" @click="openChannelComposer('list')"><Rows3 :size="16" /></button>
              </div>
              <div class="quick-reply-anchor">
              <button type="button" class="composer-tool" :class="{ active: showQuickReplies }" @click="showQuickReplies = !showQuickReplies">
                <Zap :size="16" />{{ t('quickReplies') }}
              </button>
              <div v-if="showQuickReplies" class="quick-reply-menu">
                <div class="quick-reply-search"><Search :size="14" /><input v-model="quickReplySearch" :placeholder="t('quickReplyPlaceholder')" /></div>
                <button v-for="item in filteredQuickReplies" :key="item.id" type="button" @click="insertQuickReply(item)">
                  <span><strong>/{{ item.shortcut }}</strong><em>{{ item.language }}</em></span>
                  <b>{{ item.title }}</b>
                  <p>{{ item.body }}</p>
                </button>
                <div v-if="!filteredQuickReplies.length" class="quick-reply-empty">{{ locale === 'zh-TW' ? '沒有符合的快速回覆' : '没有匹配的快捷回复' }}</div>
              </div>
            </div>
            <button class="send-button" type="submit" :disabled="sending || !messageBody.trim()" :title="composerMode === 'note' ? t('addNote') : t('send')">
              <StickyNote v-if="composerMode === 'note'" :size="18" />
              <Send v-else :size="18" />
            </button>
          </div>
        </form>
        <p v-if="errorMessage" class="composer-error">{{ errorMessage }}</p>
      </section>
    </section>

    <aside v-if="selected" class="detail-column">
      <section class="detail-section customer-profile compact-profile">
        <div class="profile-heading">
          <div class="profile-avatar">{{ selected.contact.display_name.slice(0, 1).toUpperCase() }}</div>
          <div><h3>{{ selected.contact.display_name }}</h3><p>{{ selected.contact.phone }}</p></div>
        </div>
        <label class="field-row stacked profile-name-field">
          <span>{{ locale === 'zh-TW' ? '顯示名稱' : '显示名称' }}</span>
          <input v-model="selected.contact.display_name" @input="profileDirty = true" />
        </label>
        <p class="muted-copy">{{ locale === 'zh-TW' ? `最近識別語言：${selected.contact.language}（AI 依目前訊息判斷）` : `最近识别语言：${selected.contact.language}（AI 按当前消息判断）` }}</p>
      </section>

      <section class="detail-section">
        <h4>{{ locale === 'zh-TW' ? '會話控制' : '会话控制' }}</h4>
        <label class="toggle-row">
          <span><Bot :size="16" />{{ t('ai') }}</span>
          <input type="checkbox" :checked="selected.ai_enabled" @change="updateConversation({ ai_enabled: ($event.target as HTMLInputElement).checked })" />
        </label>
        <label class="field-row">
          <span>{{ t('status') }}</span>
          <select :value="selected.status" @change="updateConversation({ status: ($event.target as HTMLSelectElement).value })">
            <option value="open">{{ t('open') }}</option>
            <option value="pending">{{ t('pending') }}</option>
            <option value="solved">{{ t('solved') }}</option>
            <option value="blocked">{{ locale === 'zh-TW' ? '已封鎖' : '已阻止' }}</option>
          </select>
        </label>
        <label class="field-row">
          <span>{{ t('priority') }}</span>
          <select :value="selected.priority" @change="updateConversation({ priority: ($event.target as HTMLSelectElement).value })">
            <option v-for="(label, value) in priorityLabel" :key="value" :value="value">{{ label }}</option>
          </select>
        </label>
      </section>

      <section v-if="latestAutomationSession" class="detail-section automation-summary">
        <div class="detail-heading-row">
          <h4><ClipboardList :size="14" />{{ automationWorkflowLabel(latestAutomationSession) }}</h4>
          <span :class="['automation-status', latestAutomationSession.status]">
            {{ automationStatusLabel(latestAutomationSession.status) }}
          </span>
        </div>
        <div class="automation-progress-heading">
          <span>{{ locale === 'zh-TW' ? '資料進度' : '资料进度' }}</span>
          <strong>{{ automationProgress }}%</strong>
        </div>
        <div class="automation-progress-track"><span :style="{ width: `${automationProgress}%` }" /></div>
        <div class="automation-field-list">
          <div
            v-for="(field, index) in automationFields"
            :key="field.key || index"
            :class="{
              complete: automationFieldAnswered(field),
              current: latestAutomationSession.status === 'active' && latestAutomationSession.current_step === index,
            }"
          >
            <span class="automation-field-state"><Check v-if="automationFieldAnswered(field)" :size="11" /><em v-else>{{ index + 1 }}</em></span>
            <p><strong>{{ automationFieldPrompt(field) }}</strong><small v-if="automationFieldAnswered(field)">{{ automationAnswer(field) }}</small></p>
          </div>
        </div>
        <p v-if="latestAutomationSession.score !== null" class="automation-score">
          {{ locale === 'zh-TW' ? '評分' : '评分' }} {{ latestAutomationSession.score }}
          <span v-if="latestAutomationSession.grade">· {{ latestAutomationSession.grade }}</span>
        </p>
      </section>

      <section v-if="identitySectionVisible" class="detail-section identity-summary">
        <div class="detail-heading-row">
          <h4><ShieldCheck :size="14" />{{ locale === 'zh-TW' ? '身份核驗' : '身份核验' }}</h4>
          <span :class="['identity-state', { verified: activeIdentityVerification }]">
            {{ activeIdentityVerification ? (locale === 'zh-TW' ? '有效' : '有效') : (locale === 'zh-TW' ? '待核驗' : '待核验') }}
          </span>
        </div>
        <div v-if="activeIdentityVerification" class="identity-verification-detail">
          <strong>{{ activeIdentityVerification.evidence_hint }}</strong>
          <span>{{ activeIdentityVerification.method }} · {{ formatDateTime(activeIdentityVerification.expires_at) }}</span>
        </div>
        <p v-else class="identity-required-copy">
          {{ locale === 'zh-TW' ? '敏感操作需先完成身份核驗。' : '敏感操作需先完成身份核验。' }}
        </p>
        <button class="profile-save-button" @click="openIdentityVerificationModal">
          <ShieldCheck :size="14" />{{ activeIdentityVerification ? (locale === 'zh-TW' ? '重新核驗' : '重新核验') : (locale === 'zh-TW' ? '記錄核驗' : '记录核验') }}
        </button>
      </section>

      <section class="detail-section">
        <h4>{{ t('assignment') }}</h4>
        <label class="field-row stacked">
          <span>{{ t('team') }}</span>
          <select :value="selected.assigned_team_id ?? ''" @change="updateConversation({ assigned_team_id: ($event.target as HTMLSelectElement).value ? Number(($event.target as HTMLSelectElement).value) : null })">
            <option value="">{{ t('unassignedValue') }}</option>
            <option v-for="team in teams" :key="team.id" :value="team.id">{{ team.name }}</option>
          </select>
        </label>
        <label class="field-row stacked">
          <span>{{ t('agent') }}</span>
          <select :value="selected.assigned_user_id ?? ''" @change="updateConversation({ assigned_user_id: ($event.target as HTMLSelectElement).value ? Number(($event.target as HTMLSelectElement).value) : null })">
            <option value="">{{ t('unassignedValue') }}</option>
            <option v-for="agent in agents" :key="agent.id" :value="agent.id">{{ agent.name }}</option>
          </select>
        </label>
      </section>

      <section class="detail-section profile-editor">
        <h4><Tag :size="13" />{{ t('tags') }}</h4>
        <div class="profile-tags editable-tags">
          <button v-for="tag in selected.contact.tags" :key="tag" title="移除標籤" @click="removeTag(tag)">{{ tag }}<X :size="11" /></button>
          <span v-if="!selected.contact.tags.length" class="muted-tag">{{ t('noTags') }}</span>
        </div>
        <div class="tag-input-row"><input v-model="tagInput" :placeholder="t('addTag')" @keydown.enter.prevent="addTag" /><button class="icon-button compact" @click="addTag"><Plus :size="14" /></button></div>
      </section>

      <section class="detail-section profile-editor">
        <div class="detail-heading-row"><h4>{{ t('customFields') }}</h4><button class="text-button" @click="addCustomField"><Plus :size="12" />{{ locale === 'zh-TW' ? '新增' : '新增' }}</button></div>
        <div class="custom-fields">
          <div v-for="(field, index) in customFields" :key="index">
            <input v-model="field.key" :placeholder="locale === 'zh-TW' ? '欄位名稱' : '字段名'" @input="profileDirty = true" />
            <input v-model="field.value" :placeholder="locale === 'zh-TW' ? '內容' : '内容'" @input="profileDirty = true" />
            <button class="icon-button compact" title="刪除" @click="customFields.splice(index, 1); profileDirty = true"><X :size="13" /></button>
          </div>
          <p v-if="!customFields.length">{{ locale === 'zh-TW' ? '尚未新增自訂欄位' : '尚未添加自定义字段' }}</p>
        </div>
        <button class="profile-save-button" :disabled="savingContact" @click="saveContactProfile"><Check :size="14" />{{ t('save') }}</button>
      </section>

      <section class="detail-section service-window">
        <h4>{{ t('serviceWindow') }}</h4>
        <div><Clock3 :size="17" /><span>{{ t('remaining') }} {{ serviceWindow }}</span></div>
        <div v-if="selected.priority === 'high' || selected.priority === 'urgent'" class="risk-line">
          <ShieldAlert :size="17" /><span>{{ priorityLabel[selected.priority] }}{{ locale === 'zh-TW' ? '優先級' : '优先级' }}</span>
        </div>
      </section>

      <section class="detail-section activity-section">
        <h4><Activity :size="13" />{{ t('activity') }}</h4>
        <div class="activity-list">
          <div v-for="item in activity" :key="item.id">
            <span class="activity-dot" />
            <p><strong>{{ item.user_name || 'RelayDesk' }}</strong>{{ activityText(item) }}</p>
            <time>{{ formatActivityTime(item.created_at) }}</time>
          </div>
          <p v-if="!activity.length" class="activity-empty">{{ locale === 'zh-TW' ? '尚無活動記錄' : '暂无活动记录' }}</p>
        </div>
      </section>
    </aside>

    <section v-if="!selected && !loading" class="inbox-empty-main">
      <Inbox :size="32" />
      <h2>{{ t('selectConversation') }}</h2>
    </section>

    <div v-if="showSimulator" class="modal-backdrop" @click.self="showSimulator = false">
      <form class="modal-panel simulator-modal" @submit.prevent="simulateInbound">
        <div class="modal-heading">
          <div><p class="section-kicker">Local demo</p><h2>模擬客戶訊息</h2></div>
          <button type="button" class="icon-button" title="關閉" @click="showSimulator = false"><X :size="19" /></button>
        </div>
        <div class="form-grid two-columns">
          <label><span>客戶姓名</span><input v-model="simulator.display_name" required /></label>
          <label><span>手機號碼</span><input v-model="simulator.phone" required /></label>
        </div>
        <label><span>訊息內容</span><textarea v-model="simulator.body" rows="4" required /></label>
        <div class="preset-row">
          <button type="button" @click="simulator.body = '請問你們的退貨期限是多久？'">知識問答</button>
          <button type="button" @click="simulator.body = '幫我查詢訂單 ORD-1001'">訂單查詢</button>
          <button type="button" @click="simulator.body = '我要投訴，請轉人工客服'">轉人工</button>
        </div>
        <div class="modal-actions">
          <button type="button" class="secondary-button" @click="showSimulator = false">取消</button>
          <button class="primary-button" type="submit" :disabled="simulating"><Sparkles :size="17" />{{ simulating ? 'Agent 處理中' : '傳送並執行 Agent' }}</button>
        </div>
      </form>
    </div>

    <div v-if="showIdentityVerification" class="modal-backdrop" @click.self="closeIdentityVerificationModal">
      <form class="modal-panel small-modal identity-verification-modal" @submit.prevent="verifyIdentity">
        <div class="modal-heading">
          <div><p class="section-kicker">Security check</p><h2>{{ locale === 'zh-TW' ? '記錄身份核驗' : '记录身份核验' }}</h2></div>
          <button type="button" class="icon-button" :title="locale === 'zh-TW' ? '關閉' : '关闭'" @click="closeIdentityVerificationModal"><X :size="19" /></button>
        </div>
        <label>
          <span>{{ locale === 'zh-TW' ? '核驗方式' : '核验方式' }}</span>
          <select v-model="identityForm.method" required>
            <option value="order_details">{{ locale === 'zh-TW' ? '訂單資料核對' : '订单资料核对' }}</option>
            <option value="registered_phone">{{ locale === 'zh-TW' ? '登記電話核對' : '登记电话核对' }}</option>
            <option value="email_otp">Email OTP</option>
            <option value="sms_otp">SMS OTP</option>
            <option value="staff_review">{{ locale === 'zh-TW' ? '人工覆核' : '人工复核' }}</option>
          </select>
        </label>
        <label>
          <span>{{ locale === 'zh-TW' ? '核驗憑據' : '核验凭据' }}</span>
          <input v-model="identityForm.evidence_reference" type="password" minlength="3" maxlength="500" autocomplete="new-password" required />
        </label>
        <label>
          <span>{{ locale === 'zh-TW' ? '稽核提示' : '审计提示' }}</span>
          <input v-model="identityForm.evidence_hint" maxlength="120" :placeholder="locale === 'zh-TW' ? '例如：電話末四碼已匹配' : '例如：电话末四码已匹配'" />
        </label>
        <label>
          <span>{{ locale === 'zh-TW' ? '有效時間' : '有效时间' }}</span>
          <select v-model.number="identityForm.expires_minutes">
            <option :value="15">15 {{ locale === 'zh-TW' ? '分鐘' : '分钟' }}</option>
            <option :value="30">30 {{ locale === 'zh-TW' ? '分鐘' : '分钟' }}</option>
            <option :value="60">60 {{ locale === 'zh-TW' ? '分鐘' : '分钟' }}</option>
            <option :value="120">120 {{ locale === 'zh-TW' ? '分鐘' : '分钟' }}</option>
          </select>
        </label>
        <p class="identity-security-note"><ShieldCheck :size="14" />{{ locale === 'zh-TW' ? '僅保存不可逆摘要，不保存憑據原文。' : '仅保存不可逆摘要，不保存凭据原文。' }}</p>
        <p v-if="errorMessage" class="composer-error">{{ errorMessage }}</p>
        <div class="modal-actions">
          <button type="button" class="secondary-button" @click="closeIdentityVerificationModal">{{ locale === 'zh-TW' ? '取消' : '取消' }}</button>
          <button class="primary-button" type="submit" :disabled="verifyingIdentity || !identityForm.evidence_reference.trim()"><ShieldCheck :size="16" />{{ verifyingIdentity ? (locale === 'zh-TW' ? '核驗中' : '核验中') : (locale === 'zh-TW' ? '確認核驗' : '确认核验') }}</button>
        </div>
      </form>
    </div>

    <div v-if="channelComposer" class="modal-backdrop" @click.self="channelComposer = null">
      <form class="modal-panel channel-message-modal" @submit.prevent="sendChannelMessage">
        <div class="modal-heading">
          <div><p class="section-kicker">WhatsApp</p><h2>{{ channelComposer === 'template' ? (locale === 'zh-TW' ? '傳送範本' : '发送模板') : channelComposer === 'buttons' ? (locale === 'zh-TW' ? '回覆按鈕' : '回复按钮') : (locale === 'zh-TW' ? '選項列表' : '选项列表') }}</h2></div>
          <button type="button" class="icon-button" :title="locale === 'zh-TW' ? '關閉' : '关闭'" @click="channelComposer = null"><X :size="19" /></button>
        </div>

        <template v-if="channelComposer === 'template'">
          <label><span>{{ locale === 'zh-TW' ? '已批准範本' : '已批准模板' }}</span><select v-model="selectedTemplateId" required><option v-for="item in whatsappTemplates" :key="item.id" :value="item.id">{{ item.name }} · {{ item.language }}</option></select></label>
          <div v-if="selectedTemplate" class="selected-template-summary"><span :class="['template-status', selectedTemplate.status.toLowerCase()]">{{ selectedTemplate.status }}</span><strong>{{ selectedTemplate.category }}</strong><small>{{ selectedTemplate.language }}</small></div>
          <div v-if="templateParameters.length" class="template-parameter-grid">
            <label v-for="(_, index) in templateParameters" :key="index"><span>{{ locale === 'zh-TW' ? '參數' : '参数' }} {{ index + 1 }}</span><input v-model="templateParameters[index]" required /></label>
          </div>
        </template>

        <template v-else>
          <div class="form-grid two-columns"><label><span>{{ locale === 'zh-TW' ? '標題' : '标题' }}</span><input v-model="interactiveForm.header" maxlength="60" /></label><label><span>{{ locale === 'zh-TW' ? '頁尾' : '页尾' }}</span><input v-model="interactiveForm.footer" maxlength="60" /></label></div>
          <label><span>{{ locale === 'zh-TW' ? '訊息內容' : '消息内容' }}</span><textarea v-model="interactiveForm.body" rows="3" maxlength="1024" required /></label>
          <template v-if="channelComposer === 'buttons'">
            <div class="interactive-editor-heading"><strong>{{ locale === 'zh-TW' ? '按鈕' : '按钮' }}</strong><button type="button" class="text-button" :disabled="interactiveForm.buttons.length >= 3" @click="addInteractiveItem"><Plus :size="13" />{{ locale === 'zh-TW' ? '新增' : '新增' }}</button></div>
            <div class="interactive-option-list"><div v-for="(button, index) in interactiveForm.buttons" :key="index"><input v-model="button.title" maxlength="20" required /><input v-model="button.id" maxlength="256" required /><button type="button" class="icon-button compact danger" title="刪除" @click="interactiveForm.buttons.splice(index, 1)"><X :size="13" /></button></div></div>
          </template>
          <template v-else>
            <div class="form-grid two-columns"><label><span>{{ locale === 'zh-TW' ? '開啟按鈕' : '打开按钮' }}</span><input v-model="interactiveForm.buttonText" maxlength="20" required /></label><label><span>{{ locale === 'zh-TW' ? '區段標題' : '分组标题' }}</span><input v-model="interactiveForm.sectionTitle" maxlength="24" required /></label></div>
            <div class="interactive-editor-heading"><strong>{{ locale === 'zh-TW' ? '列表選項' : '列表选项' }}</strong><button type="button" class="text-button" :disabled="interactiveForm.rows.length >= 10" @click="addInteractiveItem"><Plus :size="13" />{{ locale === 'zh-TW' ? '新增' : '新增' }}</button></div>
            <div class="interactive-option-list list-options"><div v-for="(row, index) in interactiveForm.rows" :key="index"><input v-model="row.title" maxlength="24" required /><input v-model="row.description" maxlength="72" :placeholder="locale === 'zh-TW' ? '說明' : '说明'" /><input v-model="row.id" maxlength="200" required /><button type="button" class="icon-button compact danger" title="刪除" @click="interactiveForm.rows.splice(index, 1)"><X :size="13" /></button></div></div>
          </template>
        </template>

        <div class="modal-actions"><button type="button" class="secondary-button" @click="channelComposer = null">{{ locale === 'zh-TW' ? '取消' : '取消' }}</button><button class="primary-button" type="submit" :disabled="channelSending"><Send :size="16" />{{ channelSending ? (locale === 'zh-TW' ? '正在傳送' : '正在发送') : (locale === 'zh-TW' ? '傳送' : '发送') }}</button></div>
      </form>
    </div>

    <Teleport to="body">
      <div
        v-if="translationMenu"
        class="message-context-menu"
        :style="{ left: `${translationMenu.x}px`, top: `${translationMenu.y}px` }"
        role="menu"
        @click.stop
      >
        <button
          type="button"
          role="menuitem"
          :disabled="!messageHasEnglish(translationMenu.message) || translatingMessageId !== null"
          @click="translateContextMessage"
        >
          <Languages :size="16" />
          <span>
            <strong>{{ translations[translationMenu.message.id] ? '重新翻譯成繁體中文' : '翻譯成繁體中文' }}</strong>
            <small>{{ messageHasEnglish(translationMenu.message) ? '只在目前頁面顯示' : '此訊息沒有英文內容' }}</small>
          </span>
        </button>
        <button
          v-if="translations[translationMenu.message.id] || translationErrors[translationMenu.message.id]"
          type="button"
          role="menuitem"
          @click="hideContextTranslation"
        >
          <EyeOff :size="16" />
          <span><strong>隱藏翻譯</strong><small>保留原始訊息</small></span>
        </button>
      </div>
    </Teleport>
  </div>
</template>
