<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  Bot,
  CheckCircle2,
  Globe2,
  History,
  ListChecks,
  Network,
  Plus,
  RotateCcw,
  Save,
  Search,
  ShieldCheck,
  Sparkles,
  Trash2,
} from '@lucide/vue'
import { api } from '../api'
import { useLocale } from '../i18n'
import type { Agent, AgentProfile, AgentProfileVersion, Bootstrap, LeadQualificationQuestion, Team } from '../types'

const props = defineProps<{ session: Bootstrap }>()
const { locale } = useLocale()

type EditableProfile = Omit<AgentProfileVersion, 'id' | 'version_number' | 'status' | 'created_by_user_id' | 'published_by_user_id' | 'rollback_from_version_id' | 'created_at' | 'updated_at' | 'published_at' | 'generation_summary'> & {
  generation_summary?: string | null
}

const profile = ref<AgentProfile | null>(null)
const versions = ref<AgentProfileVersion[]>([])
const teams = ref<Team[]>([])
const agents = ref<Agent[]>([])
const form = ref<EditableProfile | null>(null)
const website = ref('')
const loading = ref(false)
const saving = ref(false)
const generating = ref(false)
const publishing = ref(false)
const rollingBack = ref<number | null>(null)
const error = ref('')
const notice = ref('')

const canEdit = computed(() => ['admin', 'manager'].includes(props.session.user.role))
const canPublish = computed(() => props.session.user.role === 'admin')
const active = computed(() => profile.value?.active_version || null)
const draft = computed(() => profile.value?.draft_version || null)

function versionToForm(version: AgentProfileVersion | null): EditableProfile {
  return {
    identity: version?.identity || '',
    service_scope: [...(version?.service_scope || [])],
    tone: version?.tone || '',
    knowledge_priority: [...(version?.knowledge_priority || [])],
    prohibitions: [...(version?.prohibitions || [])],
    handoff_conditions: [...(version?.handoff_conditions || [])],
    reply_language: 'auto',
    fallback_language: version?.fallback_language || 'zh-TW',
    order_intake_enabled: version?.order_intake_enabled ?? true,
    automation_timeout_minutes: version?.automation_timeout_minutes || 30,
    web_search_enabled: version?.web_search_enabled ?? false,
    web_search_allowed_domains: [...(version?.web_search_allowed_domains || [])],
    lead_qualification: JSON.parse(JSON.stringify(version?.lead_qualification || {
      enabled: false,
      trigger_terms: [],
      questions: [],
      grades: [],
    })),
    instructions: version?.instructions || '',
    source_url: version?.source_url || null,
    generation_summary: version?.generation_summary || null,
  }
}

function listText(values: string[]) {
  return values.join('\n')
}

function parseList(value: string) {
  return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
}

function updateList(field: 'service_scope' | 'knowledge_priority' | 'prohibitions' | 'handoff_conditions', event: Event) {
  if (!form.value) return
  form.value[field] = parseList((event.target as HTMLTextAreaElement).value)
}

function updateSearchDomains(event: Event) {
  if (!form.value) return
  form.value.web_search_allowed_domains = parseList((event.target as HTMLTextAreaElement).value)
}

function updateLeadTriggers(event: Event) {
  if (!form.value) return
  form.value.lead_qualification.trigger_terms = parseList((event.target as HTMLTextAreaElement).value)
}

function addLeadQuestion() {
  if (!form.value) return
  const existing = new Set(form.value.lead_qualification.questions.map((item) => item.id))
  let index = form.value.lead_qualification.questions.length + 1
  while (existing.has(`question_${index}`)) index += 1
  form.value.lead_qualification.questions.push({
    id: `question_${index}`,
    prompt: '',
    prompt_en: null,
    kind: 'text',
    required: true,
    default_score: 0,
    options: [],
  })
}

function changeQuestionKind(question: LeadQualificationQuestion) {
  if (question.kind === 'single_choice' && !question.options.length) {
    question.options.push({ value: 'option_1', label: '', score: 0 })
  }
  if (question.kind !== 'single_choice') question.options = []
}

function addLeadOption(question: LeadQualificationQuestion) {
  const index = question.options.length + 1
  question.options.push({ value: `option_${index}`, label: '', score: 0 })
}

function addLeadGrade() {
  if (!form.value) return
  const index = form.value.lead_qualification.grades.length + 1
  form.value.lead_qualification.grades.push({
    name: `level_${index}`,
    min_score: 0,
    tag: null,
    priority: null,
    team_id: null,
    user_id: null,
  })
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [loaded, history, loadedTeams, loadedAgents] = await Promise.all([
      api.get<AgentProfile>('/api/ai-agent'),
      api.get<AgentProfileVersion[]>('/api/ai-agent/versions'),
      api.get<Team[]>('/api/teams'),
      api.get<Agent[]>('/api/agents'),
    ])
    profile.value = loaded
    versions.value = history
    teams.value = loadedTeams
    agents.value = loadedAgents
    form.value = versionToForm(loaded.draft_version || loaded.active_version)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : (locale.value === 'zh-TW' ? '無法讀取 AI 代理設定' : '无法读取 AI 代理设置')
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!form.value) return
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    const saved = await api.patch<AgentProfileVersion>('/api/ai-agent/draft', form.value)
    profile.value = profile.value ? { ...profile.value, draft_version: saved } : profile.value
    form.value = versionToForm(saved)
    notice.value = locale.value === 'zh-TW' ? '草稿已儲存，等待管理員發布' : '草稿已保存，等待管理员发布'
    await loadVersions()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : (locale.value === 'zh-TW' ? '儲存失敗' : '保存失败')
  } finally {
    saving.value = false
  }
}

async function generate() {
  if (!website.value.trim()) return
  generating.value = true
  error.value = ''
  notice.value = ''
  try {
    const generated = await api.post<AgentProfileVersion>('/api/ai-agent/generate', { source_url: website.value.trim() })
    profile.value = profile.value ? { ...profile.value, draft_version: generated } : profile.value
    form.value = versionToForm(generated)
    notice.value = locale.value === 'zh-TW'
      ? '知識庫頁面草稿與代理指令草稿已生成，等待審核'
      : '知识库页面草稿与代理指令草稿已生成，等待审核'
    await loadVersions()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : (locale.value === 'zh-TW' ? '網站分析失敗' : '网站分析失败')
  } finally {
    generating.value = false
  }
}

async function publish() {
  publishing.value = true
  error.value = ''
  try {
    const result = await api.post<{ active_version: AgentProfileVersion }>('/api/ai-agent/publish')
    profile.value = profile.value ? { ...profile.value, active_version: result.active_version, draft_version: null } : profile.value
    form.value = versionToForm(result.active_version)
    notice.value = locale.value === 'zh-TW' ? '已發布，新的 AI 回覆將使用此版本' : '已发布，新的 AI 回复将使用此版本'
    await loadVersions()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : (locale.value === 'zh-TW' ? '發布失敗' : '发布失败')
  } finally {
    publishing.value = false
  }
}

async function loadVersions() {
  versions.value = await api.get<AgentProfileVersion[]>('/api/ai-agent/versions')
}

async function rollback(version: AgentProfileVersion) {
  if (!canPublish.value || rollingBack.value !== null) return
  const prompt = locale.value === 'zh-TW' ? `回退到 v${version.version_number}？` : `回退到 v${version.version_number}？`
  if (!window.confirm(prompt)) return
  rollingBack.value = version.id
  error.value = ''
  try {
    const result = await api.post<{ active_version: AgentProfileVersion }>(`/api/ai-agent/versions/${version.id}/rollback`)
    profile.value = profile.value ? { ...profile.value, active_version: result.active_version, draft_version: null } : profile.value
    form.value = versionToForm(result.active_version)
    notice.value = locale.value === 'zh-TW' ? '版本已回退' : '版本已回退'
    await loadVersions()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : (locale.value === 'zh-TW' ? '回退失敗' : '回退失败')
  } finally {
    rollingBack.value = null
  }
}

function formatDate(value: string | null) {
  if (!value) return '-'
  return new Date(value).toLocaleString(locale.value === 'zh-TW' ? 'zh-HK' : 'zh-CN', { dateStyle: 'short', timeStyle: 'short' })
}

onMounted(load)
</script>

<template>
  <div class="page-view ai-agent-view">
    <section class="page-toolbar">
      <div><p class="section-kicker">AI agent</p><h2>{{ locale === 'zh-TW' ? 'AI 代理設定' : 'AI 代理配置' }}</h2><span>{{ locale === 'zh-TW' ? '草稿必須經管理員審核後才會影響客戶回覆' : '草稿必须经管理员审核后才会影响客户回复' }}</span></div>
      <div class="toolbar-actions"><span v-if="active" class="status-pill"><CheckCircle2 :size="14" />v{{ active.version_number }} {{ locale === 'zh-TW' ? '已發布' : '已发布' }}</span></div>
    </section>

    <p v-if="loading" class="empty-state">{{ locale === 'zh-TW' ? '載入中…' : '加载中…' }}</p>
    <p v-if="error" class="form-error">{{ error }}</p>
    <p v-if="notice" class="form-success">{{ notice }}</p>

    <template v-if="form">
      <section class="panel ai-agent-generate-panel">
        <div class="settings-heading"><Sparkles :size="20" /><div><h3>{{ locale === 'zh-TW' ? '由網站生成雙草稿' : '从网站生成双草稿' }}</h3><span>{{ locale === 'zh-TW' ? '同時建立知識庫頁面與代理指令草稿，審核前不會發布' : '同时建立知识库页面与代理指令草稿，审核前不会发布' }}</span></div></div>
        <div class="generate-row"><label><span>Website URL</span><input v-model="website" type="url" placeholder="https://example.com" /></label><button class="secondary-button" type="button" :disabled="generating || !canEdit" @click="generate"><Globe2 :size="16" />{{ generating ? (locale === 'zh-TW' ? '分析中' : '分析中') : (locale === 'zh-TW' ? '生成草稿' : '生成草稿') }}</button></div>
        <small v-if="draft?.generation_summary" class="muted-copy">{{ draft.generation_summary }}</small>
      </section>

      <section class="panel ai-agent-form-panel">
        <div class="settings-heading"><Bot :size="20" /><div><h3>{{ locale === 'zh-TW' ? '代理行為' : '代理行为' }}</h3><span>{{ locale === 'zh-TW' ? '編輯後先儲存草稿，再由管理員發布' : '编辑后先保存草稿，再由管理员发布' }}</span></div></div>
        <div class="ai-agent-form-grid">
          <label><span>{{ locale === 'zh-TW' ? '身份' : '身份' }}</span><textarea v-model="form.identity" rows="3" /></label>
          <label><span>{{ locale === 'zh-TW' ? '語氣' : '语气' }}</span><textarea v-model="form.tone" rows="3" /></label>
          <label><span>{{ locale === 'zh-TW' ? '服務範圍（每行一項）' : '服务范围（每行一项）' }}</span><textarea :value="listText(form.service_scope)" rows="5" @input="updateList('service_scope', $event)" /></label>
          <label><span>{{ locale === 'zh-TW' ? '知識優先級（每行一項）' : '知识优先级（每行一项）' }}</span><textarea :value="listText(form.knowledge_priority)" rows="5" @input="updateList('knowledge_priority', $event)" /></label>
          <label><span>{{ locale === 'zh-TW' ? '禁止事項（每行一項）' : '禁止事项（每行一项）' }}</span><textarea :value="listText(form.prohibitions)" rows="5" @input="updateList('prohibitions', $event)" /></label>
          <label><span>{{ locale === 'zh-TW' ? '轉人工條件（每行一項）' : '转人工条件（每行一项）' }}</span><textarea :value="listText(form.handoff_conditions)" rows="5" @input="updateList('handoff_conditions', $event)" /></label>
        </div>
        <div class="language-policy"><div><ShieldCheck :size="17" /><strong>{{ locale === 'zh-TW' ? '回覆語言' : '回复语言' }}</strong><span>{{ locale === 'zh-TW' ? '自動識別客戶當前訊息，不使用聯絡人手動語言' : '自动识别客户当前消息，不使用联系人手动语言' }}</span></div><select v-model="form.fallback_language"><option value="zh-TW">繁體中文</option><option value="zh-CN">简体中文</option><option value="en">English</option></select></div>
        <div class="agent-instructions-preview"><strong>{{ locale === 'zh-TW' ? '代理指令預覽' : '代理指令预览' }}</strong><pre>{{ form.instructions }}</pre></div>
        <div class="modal-actions"><button class="secondary-button" type="button" :disabled="saving || !canEdit" @click="save"><Save :size="16" />{{ saving ? (locale === 'zh-TW' ? '儲存中' : '保存中') : (locale === 'zh-TW' ? '儲存草稿' : '保存草稿') }}</button><button v-if="canPublish" class="primary-button" type="button" :disabled="publishing || !draft" @click="publish"><CheckCircle2 :size="16" />{{ publishing ? (locale === 'zh-TW' ? '發布中' : '发布中') : (locale === 'zh-TW' ? '審核並發布' : '审核并发布') }}</button></div>
      </section>

      <section class="panel ai-agent-automation-panel">
        <div class="settings-heading"><Network :size="20" /><div><h3>{{ locale === 'zh-TW' ? '業務自動化' : '业务自动化' }}</h3><span>LangGraph</span></div></div>
        <div class="automation-policy-grid">
          <label class="toggle-row"><span>{{ locale === 'zh-TW' ? '訂單資料逐項收集' : '订单资料逐项收集' }}</span><input v-model="form.order_intake_enabled" type="checkbox" :disabled="!canEdit" /></label>
          <label class="compact-field"><span>{{ locale === 'zh-TW' ? '表單逾時（分鐘）' : '表单超时（分钟）' }}</span><input v-model.number="form.automation_timeout_minutes" type="number" min="5" max="1440" :disabled="!canEdit" /></label>
          <label class="toggle-row"><span>{{ locale === 'zh-TW' ? '知識不足時允許網絡搜尋' : '知识不足时允许网络搜索' }}</span><input v-model="form.web_search_enabled" type="checkbox" :disabled="!canEdit" /></label>
          <label class="compact-field automation-domains"><span>{{ locale === 'zh-TW' ? '允許引用域名（每行一個，留空代表不限）' : '允许引用域名（每行一个，留空代表不限）' }}</span><textarea :value="listText(form.web_search_allowed_domains)" rows="3" :disabled="!canEdit || !form.web_search_enabled" @input="updateSearchDomains" /></label>
        </div>
      </section>

      <section class="panel ai-agent-lead-panel">
        <div class="lead-panel-heading">
          <div class="settings-heading"><ListChecks :size="20" /><div><h3>{{ locale === 'zh-TW' ? '線索資格' : '线索资格' }}</h3><span>{{ form.lead_qualification.questions.length }} questions · {{ form.lead_qualification.grades.length }} levels</span></div></div>
          <label class="toggle-row compact-toggle"><span>{{ locale === 'zh-TW' ? '啟用' : '启用' }}</span><input v-model="form.lead_qualification.enabled" type="checkbox" :disabled="!canEdit" /></label>
        </div>
        <label class="compact-field lead-trigger-field"><span>{{ locale === 'zh-TW' ? '觸發語句（每行一項）' : '触发语句（每行一项）' }}</span><textarea :value="listText(form.lead_qualification.trigger_terms)" rows="2" :disabled="!canEdit" @input="updateLeadTriggers" /></label>

        <div class="automation-subheading"><strong>{{ locale === 'zh-TW' ? '資格問題' : '资格问题' }}</strong><button class="secondary-button compact-button" type="button" :disabled="!canEdit || form.lead_qualification.questions.length >= 20" @click="addLeadQuestion"><Plus :size="14" />{{ locale === 'zh-TW' ? '新增問題' : '新增问题' }}</button></div>
        <div class="lead-question-list">
          <div v-for="(question, questionIndex) in form.lead_qualification.questions" :key="questionIndex" class="lead-question-row">
            <div class="lead-row-index">{{ questionIndex + 1 }}</div>
            <div class="lead-question-fields">
              <div class="form-grid lead-question-main"><label><span>ID</span><input v-model="question.id" maxlength="64" :disabled="!canEdit" /></label><label><span>{{ locale === 'zh-TW' ? '類型' : '类型' }}</span><select v-model="question.kind" :disabled="!canEdit" @change="changeQuestionKind(question)"><option value="text">Text</option><option value="number">Number</option><option value="single_choice">Single choice</option></select></label><label><span>{{ locale === 'zh-TW' ? '基礎分' : '基础分' }}</span><input v-model.number="question.default_score" type="number" min="-100" max="100" :disabled="!canEdit" /></label></div>
              <div class="form-grid two-columns"><label><span>{{ locale === 'zh-TW' ? '問題' : '问题' }}</span><input v-model="question.prompt" maxlength="500" :disabled="!canEdit" /></label><label><span>English</span><input v-model="question.prompt_en" maxlength="500" :disabled="!canEdit" /></label></div>
              <div v-if="question.kind === 'single_choice'" class="lead-option-list"><div v-for="(option, optionIndex) in question.options" :key="optionIndex"><input v-model="option.value" maxlength="80" placeholder="value" :disabled="!canEdit" /><input v-model="option.label" maxlength="160" :placeholder="locale === 'zh-TW' ? '顯示文字' : '显示文字'" :disabled="!canEdit" /><input v-model.number="option.score" type="number" min="-100" max="100" :disabled="!canEdit" /><button class="icon-button compact danger" type="button" title="刪除" :disabled="!canEdit || question.options.length <= 1" @click="question.options.splice(optionIndex, 1)"><Trash2 :size="13" /></button></div><button class="text-button" type="button" :disabled="!canEdit || question.options.length >= 20" @click="addLeadOption(question)"><Plus :size="13" />{{ locale === 'zh-TW' ? '選項' : '选项' }}</button></div>
            </div>
            <button class="icon-button danger" type="button" :title="locale === 'zh-TW' ? '刪除問題' : '删除问题'" :disabled="!canEdit" @click="form.lead_qualification.questions.splice(questionIndex, 1)"><Trash2 :size="15" /></button>
          </div>
          <div v-if="!form.lead_qualification.questions.length" class="empty-state compact-state"><Search :size="20" />{{ locale === 'zh-TW' ? '尚未配置資格問題' : '尚未配置资格问题' }}</div>
        </div>

        <div class="automation-subheading"><strong>{{ locale === 'zh-TW' ? '評分等級與分配' : '评分等级与分配' }}</strong><button class="secondary-button compact-button" type="button" :disabled="!canEdit || form.lead_qualification.grades.length >= 10" @click="addLeadGrade"><Plus :size="14" />{{ locale === 'zh-TW' ? '新增等級' : '新增等级' }}</button></div>
        <div class="lead-grade-table">
          <div class="lead-grade-head"><span>{{ locale === 'zh-TW' ? '等級' : '等级' }}</span><span>Min score</span><span>Tag</span><span>{{ locale === 'zh-TW' ? '優先級' : '优先级' }}</span><span>{{ locale === 'zh-TW' ? '團隊' : '团队' }}</span><span>{{ locale === 'zh-TW' ? '客服' : '客服' }}</span><span /></div>
          <div v-for="(grade, gradeIndex) in form.lead_qualification.grades" :key="gradeIndex" class="lead-grade-row"><input v-model="grade.name" maxlength="80" :disabled="!canEdit" /><input v-model.number="grade.min_score" type="number" :disabled="!canEdit" /><input v-model="grade.tag" maxlength="80" :disabled="!canEdit" /><select v-model="grade.priority" :disabled="!canEdit"><option :value="null">-</option><option value="low">Low</option><option value="normal">Normal</option><option value="high">High</option><option value="urgent">Urgent</option></select><select v-model="grade.team_id" :disabled="!canEdit"><option :value="null">-</option><option v-for="team in teams" :key="team.id" :value="team.id">{{ team.name }}</option></select><select v-model="grade.user_id" :disabled="!canEdit"><option :value="null">-</option><option v-for="agent in agents" :key="agent.id" :value="agent.id">{{ agent.name }}</option></select><button class="icon-button compact danger" type="button" title="刪除" :disabled="!canEdit" @click="form.lead_qualification.grades.splice(gradeIndex, 1)"><Trash2 :size="13" /></button></div>
          <div v-if="!form.lead_qualification.grades.length" class="empty-state compact-state">{{ locale === 'zh-TW' ? '尚未配置評分等級' : '尚未配置评分等级' }}</div>
        </div>
      </section>

      <section class="panel ai-agent-history-panel">
        <div class="settings-heading"><History :size="20" /><div><h3>{{ locale === 'zh-TW' ? '版本歷史' : '版本历史' }}</h3><span>{{ locale === 'zh-TW' ? '回退會建立新的發布版本，保留完整紀錄' : '回退会创建新的发布版本，保留完整记录' }}</span></div></div>
        <div class="version-list"><article v-for="version in versions" :key="version.id" :class="['version-row', version.status]"><div><strong>v{{ version.version_number }}</strong><span>{{ version.status === 'published' ? (locale === 'zh-TW' ? '已發布' : '已发布') : version.status === 'draft' ? (locale === 'zh-TW' ? '待審核' : '待审核') : (locale === 'zh-TW' ? '歷史' : '历史') }}</span></div><p>{{ version.identity }}</p><time>{{ formatDate(version.published_at || version.updated_at) }}</time><button v-if="canPublish && version.status !== 'published' && version.status !== 'draft'" class="icon-button" :title="locale === 'zh-TW' ? '回退到此版本' : '回退到此版本'" :disabled="rollingBack !== null" @click="rollback(version)"><RotateCcw :size="15" /></button></article><div v-if="!versions.length" class="empty-state">{{ locale === 'zh-TW' ? '尚無版本' : '暂无版本' }}</div></div>
      </section>
    </template>
  </div>
</template>
