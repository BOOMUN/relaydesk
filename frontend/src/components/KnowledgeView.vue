<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  BookOpenText,
  CheckCircle2,
  CircleAlert,
  Clock3,
  FileText,
  Globe2,
  Link2,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
  UploadCloud,
  X,
} from '@lucide/vue'
import { api } from '../api'
import { useLocale } from '../i18n'
import type { KnowledgeDocument, KnowledgeSource } from '../types'

const documents = ref<KnowledgeDocument[]>([])
const sources = ref<KnowledgeSource[]>([])
const { locale } = useLocale()
const search = ref('')
const reviewFilter = ref<'all' | 'draft' | 'published'>('all')
const categoryFilter = ref('all')
const editing = ref<KnowledgeDocument | null>(null)
const creating = ref(false)
const saving = ref(false)
const learning = ref(false)
const sourceAction = ref<number | null>(null)
const errorMessage = ref('')
const form = ref({
  title: '',
  category: 'general',
  source: 'manual',
  content: '',
  review_status: 'published' as 'draft' | 'published',
  pending_revision_id: null as number | null,
})
const sourceForm = ref({ root_url: '', max_pages: 500, max_depth: 5 })
let pollTimer: number | undefined

const categoryOptions = [
  { value: 'general', cn: '通用', tw: '通用' },
  { value: 'product', cn: '产品', tw: '產品' },
  { value: 'faq', cn: '常见问题', tw: '常見問題' },
  { value: 'policy', cn: '政策条款', tw: '政策條款' },
  { value: 'orders', cn: '订单物流', tw: '訂單物流' },
  { value: 'after_sales', cn: '售后服务', tw: '售後服務' },
  { value: 'service', cn: '客服服务', tw: '客服服務' },
  { value: 'company', cn: '公司信息', tw: '公司資訊' },
  { value: 'other', cn: '其他', tw: '其他' },
]

const categoryLabel = computed<Record<string, string>>(() => Object.fromEntries(
  categoryOptions.map((item) => [item.value, locale.value === 'zh-TW' ? item.tw : item.cn]),
))

const sourceStatusLabel = computed<Record<string, string>>(() => ({
  queued: locale.value === 'zh-TW' ? '等待同步' : '等待同步',
  running: locale.value === 'zh-TW' ? '正在同步' : '正在同步',
  completed: locale.value === 'zh-TW' ? '同步完成' : '同步完成',
  partial: locale.value === 'zh-TW' ? '部分同步' : '部分同步',
  failed: locale.value === 'zh-TW' ? '同步失敗' : '同步失败',
}))

const filtered = computed(() => {
  const query = search.value.trim().toLowerCase()
  return documents.value.filter((item) => {
    if (reviewFilter.value === 'draft' && item.review_status !== 'draft' && !item.pending_update) return false
    if (reviewFilter.value === 'published' && item.review_status !== 'published') return false
    if (categoryFilter.value !== 'all' && item.category !== categoryFilter.value) return false
    if (!query) return true
    return `${item.title} ${item.category} ${item.content} ${item.source_url || item.source}`.toLowerCase().includes(query)
  })
})

const activeCount = computed(() => documents.value.filter((item) => item.is_active).length)
const draftCount = computed(() => documents.value.filter((item) => item.review_status === 'draft' || item.pending_update).length)
const discoveredCount = computed(() => sources.value.reduce((total, source) => total + source.discovered_pages, 0) || documents.value.length)
const importedCount = computed(() => sources.value.reduce((total, source) => total + source.imported_pages, 0) || documents.value.length)
const publishedCount = computed(() => documents.value.filter((item) => item.review_status === 'published' && !item.pending_update).length)
const failedCount = computed(() => sources.value.reduce((total, source) => total + source.failed_pages + source.failed_task_count, 0))
const qualityHints = computed(() => {
  const titleCounts = new Map<string, number>()
  documents.value.forEach((item) => {
    const key = item.title.trim().toLocaleLowerCase()
    titleCounts.set(key, (titleCounts.get(key) || 0) + 1)
  })
  const duplicateTitles = documents.value.filter((item) => (titleCounts.get(item.title.trim().toLocaleLowerCase()) || 0) > 1).length
  const shortContent = documents.value.filter((item) => item.word_count < 80).length
  const navigationNoise = documents.value.filter((item) => /(^|\s)(menu|footer|header|cookie|navigation|导航|菜单|頁尾|頁首)/i.test(item.title)).length
  return [
    { key: 'short', label: locale.value === 'zh-TW' ? '內容過短' : '内容过短', count: shortContent, tone: 'amber' },
    { key: 'duplicate', label: locale.value === 'zh-TW' ? '重複內容' : '重复内容', count: duplicateTitles, tone: 'red' },
    { key: 'noise', label: locale.value === 'zh-TW' ? '導航噪聲' : '导航噪声', count: navigationNoise, tone: 'slate' },
  ]
})
const hasRunningSource = computed(() => sources.value.some((item) => ['queued', 'running'].includes(item.status)))

async function loadDocuments() {
  documents.value = await api.get<KnowledgeDocument[]>('/api/knowledge')
}

async function loadSources() {
  sources.value = await api.get<KnowledgeSource[]>('/api/knowledge/sources')
}

async function loadAll() {
  await Promise.all([loadDocuments(), loadSources()])
}

function openCreate() {
  editing.value = null
  form.value = { title: '', category: 'general', source: 'manual', content: '', review_status: 'published', pending_revision_id: null }
  creating.value = true
}

function openEdit(document: KnowledgeDocument) {
  editing.value = document
  form.value = {
    title: document.pending_title || document.title,
    category: document.pending_category || document.category,
    source: document.source,
    content: document.pending_content || document.content,
    review_status: document.pending_update ? 'draft' : document.review_status,
    pending_revision_id: document.pending_revision_id,
  }
  creating.value = true
}

async function saveDocument() {
  saving.value = true
  errorMessage.value = ''
  try {
    if (editing.value) {
      await api.patch(`/api/knowledge/${editing.value.id}`, form.value)
    } else {
      const { review_status: _, pending_revision_id: __, ...payload } = form.value
      await api.post('/api/knowledge', payload)
    }
    creating.value = false
    await loadDocuments()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    saving.value = false
  }
}

async function startLearning() {
  learning.value = true
  errorMessage.value = ''
  try {
    await api.post<KnowledgeSource>('/api/knowledge/sources', sourceForm.value)
    sourceForm.value.root_url = ''
    await loadSources()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    learning.value = false
  }
}

async function publishSource(source: KnowledgeSource) {
  sourceAction.value = source.id
  errorMessage.value = ''
  try {
    await api.post(`/api/knowledge/sources/${source.id}/publish`)
    await loadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    sourceAction.value = null
  }
}

async function syncSource(source: KnowledgeSource) {
  sourceAction.value = source.id
  errorMessage.value = ''
  try {
    await api.post(`/api/knowledge/sources/${source.id}/sync`)
    await loadSources()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    sourceAction.value = null
  }
}

async function removeSource(source: KnowledgeSource) {
  const prompt = locale.value === 'zh-TW'
    ? `刪除「${source.domain}」及其採集的全部知識文件？`
    : `删除“${source.domain}”及其采集的全部知识文档？`
  if (!window.confirm(prompt)) return
  sourceAction.value = source.id
  try {
    await api.delete(`/api/knowledge/sources/${source.id}`)
    await loadAll()
  } finally {
    sourceAction.value = null
  }
}

async function removeDocument(document: KnowledgeDocument) {
  const prompt = locale.value === 'zh-TW' ? `刪除知識文件「${document.title}」？` : `删除知识文档“${document.title}”？`
  if (!window.confirm(prompt)) return
  await api.delete(`/api/knowledge/${document.id}`)
  await loadAll()
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat(locale.value === 'zh-TW' ? 'zh-TW' : 'zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

function formatSchedule(value: string) {
  return new Intl.DateTimeFormat(locale.value === 'zh-TW' ? 'zh-TW' : 'zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function progress(source: KnowledgeSource) {
  if (['completed', 'partial', 'failed'].includes(source.status)) return 100
  if (source.status === 'queued') return 2
  const handledPages = source.imported_pages + source.failed_pages
  const knownPages = Math.max(source.discovered_pages, handledPages, 1)
  return Math.min(95, Math.max(5, Math.round((handledPages / knownPages) * 100)))
}

onMounted(async () => {
  await loadAll()
  pollTimer = window.setInterval(async () => {
    if (!hasRunningSource.value) return
    await loadAll()
  }, 2500)
})

onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="page-view knowledge-view">
    <section class="page-toolbar">
      <div>
        <p class="section-kicker">Website learning & retrieval</p>
        <h2>{{ locale === 'zh-TW' ? '知識庫' : '知识库' }}</h2>
      </div>
      <button class="primary-button" @click="openCreate">
        <Plus :size="17" />{{ locale === 'zh-TW' ? '手動新增' : '手动添加' }}
      </button>
    </section>

    <section class="knowledge-ingest-panel">
      <div class="ingest-heading">
        <div class="ingest-icon"><Globe2 :size="22" /></div>
        <div>
          <p class="section-kicker">Learn from website</p>
          <h3>{{ locale === 'zh-TW' ? '輸入網址，建立網站知識' : '输入网址，建立网站知识' }}</h3>
          <p>{{ locale === 'zh-TW' ? '採集同域公開 HTML 與 PDF，自動分類並先存為待審核內容。' : '采集同域公开 HTML 与 PDF，自动分类并先存为待审核内容。' }}</p>
        </div>
      </div>
      <form class="crawl-form" @submit.prevent="startLearning">
        <label class="crawl-url-field">
          <span>{{ locale === 'zh-TW' ? '網站網址' : '网站网址' }}</span>
          <div><Link2 :size="16" /><input v-model.trim="sourceForm.root_url" type="url" required placeholder="https://www.example.com/" /></div>
        </label>
        <label>
          <span>{{ locale === 'zh-TW' ? '最多頁數' : '最多页数' }}</span>
          <input v-model.number="sourceForm.max_pages" type="number" min="1" max="500" required />
        </label>
        <label>
          <span>{{ locale === 'zh-TW' ? '連結深度' : '链接深度' }}</span>
          <input v-model.number="sourceForm.max_depth" type="number" min="0" max="5" required />
        </label>
        <button class="primary-button learn-button" type="submit" :disabled="learning">
          <RefreshCw v-if="learning" class="spin" :size="16" />
          <UploadCloud v-else :size="17" />
          {{ learning ? (locale === 'zh-TW' ? '正在建立任務' : '正在建立任务') : (locale === 'zh-TW' ? '開始學習' : '开始学习') }}
        </button>
      </form>
      <div class="crawl-safety-note">
        <ShieldCheck :size="15" />
        <span>{{ locale === 'zh-TW' ? '僅允許公開網路的 80/443 連接埠；每日北京時間 03:10 自動比對，變更內容先進入待審核。' : '仅允许公网的 80/443 端口；每天北京时间 03:10 自动比对，变化内容先进入待审核。' }}</span>
      </div>
    </section>

    <div v-if="errorMessage" class="page-error"><CircleAlert :size="16" />{{ errorMessage }}</div>

    <section v-if="sources.length" class="knowledge-sources">
      <div class="source-section-heading">
        <div><p class="section-kicker">Website sources</p><h3>{{ locale === 'zh-TW' ? '網站來源' : '网站来源' }}</h3></div>
        <button class="icon-button" :title="locale === 'zh-TW' ? '重新整理' : '刷新'" @click="loadAll"><RefreshCw :size="16" /></button>
      </div>
      <article v-for="source in sources" :key="source.id" class="knowledge-source-card">
        <div class="source-domain-icon"><Globe2 :size="18" /></div>
        <div class="source-main">
          <div class="source-title-row">
            <a :href="source.root_url" target="_blank" rel="noreferrer">{{ source.domain }}</a>
            <span :class="['crawl-status', `status-${source.status}`]">
              <RefreshCw v-if="['queued', 'running'].includes(source.status)" class="spin" :size="12" />
              <CheckCircle2 v-else-if="source.status === 'completed'" :size="12" />
              <CircleAlert v-else :size="12" />
              {{ sourceStatusLabel[source.status] }}
            </span>
          </div>
          <p class="source-url">{{ source.root_url }}</p>
          <div class="crawl-progress"><span :style="{ width: `${progress(source)}%` }" /></div>
          <div class="source-metrics">
            <span>{{ locale === 'zh-TW' ? '發現' : '发现' }} <strong>{{ source.discovered_pages }}</strong></span>
            <span>{{ locale === 'zh-TW' ? '已匯入' : '已导入' }} <strong>{{ source.imported_pages }}</strong></span>
            <span>{{ locale === 'zh-TW' ? '待審核' : '待审核' }} <strong>{{ source.draft_pages }}</strong></span>
            <span>{{ locale === 'zh-TW' ? '已發布' : '已发布' }} <strong>{{ source.published_pages }}</strong></span>
            <span v-if="source.pending_updates">{{ locale === 'zh-TW' ? '更新待審' : '更新待审' }} <strong>{{ source.pending_updates }}</strong></span>
            <span v-if="source.suspected_removed_pages">{{ locale === 'zh-TW' ? '疑似下線' : '疑似下线' }} <strong>{{ source.suspected_removed_pages }}</strong></span>
            <span v-if="source.failed_pages">{{ locale === 'zh-TW' ? '失敗' : '失败' }} <strong>{{ source.failed_pages }}</strong></span>
          </div>
          <div class="source-sync-summary">
            <span><Clock3 :size="13" />{{ locale === 'zh-TW' ? `每日 ${source.sync_time}（北京時間）` : `每天 ${source.sync_time}（北京时间）` }}</span>
            <span v-if="source.last_successful_sync_at" class="sync-success-notice">{{ locale === 'zh-TW' ? '上次成功' : '上次成功' }} {{ formatSchedule(source.last_successful_sync_at) }}</span>
            <span v-else>{{ locale === 'zh-TW' ? '尚無成功同步記錄' : '尚无成功同步记录' }}</span>
            <span>{{ locale === 'zh-TW' ? '下次同步' : '下次同步' }} {{ formatSchedule(source.next_sync_at) }}</span>
            <span v-if="source.last_sync_trigger">{{ locale === 'zh-TW' ? '上次差異' : '上次差异' }}：+{{ source.last_new_pages }} / Δ{{ source.last_changed_pages }} / ={{ source.last_unchanged_pages }}</span>
            <span v-if="source.failed_task_count || source.partial_task_count" class="sync-failure-notice">
              {{ locale === 'zh-TW' ? '失敗任務' : '失败任务' }} {{ source.failed_task_count }}
              <template v-if="source.partial_task_count"> · {{ locale === 'zh-TW' ? '部分完成' : '部分完成' }} {{ source.partial_task_count }}</template>
              <template v-if="source.last_failed_task_at"> · {{ formatSchedule(source.last_failed_task_at) }}</template>
            </span>
            <span v-if="source.next_retry_at" class="retry-notice">{{ locale === 'zh-TW' ? '自動重試' : '自动重试' }} {{ formatSchedule(source.next_retry_at) }}</span>
          </div>
          <p v-if="source.error_message" class="source-error">{{ source.error_message }}</p>
          <p v-if="source.last_failure_message && source.last_failure_message !== source.error_message" class="source-error">
            {{ locale === 'zh-TW' ? '最近失敗：' : '最近失败：' }}{{ source.last_failure_message }}
          </p>
        </div>
        <div class="source-actions">
          <button
            v-if="source.draft_pages && !['queued', 'running'].includes(source.status)"
            class="primary-button compact-button"
            :disabled="sourceAction === source.id"
            @click="publishSource(source)"
          >
            <CheckCircle2 :size="14" />{{ locale === 'zh-TW' ? '發布全部' : '发布全部' }}
          </button>
          <button
            v-if="['failed', 'partial', 'completed'].includes(source.status)"
            class="secondary-button compact-button"
            :disabled="sourceAction === source.id"
            @click="syncSource(source)"
          >
            <RefreshCw :size="14" />{{ locale === 'zh-TW' ? '立即同步' : '立即同步' }}
          </button>
          <button
            v-if="!['queued', 'running'].includes(source.status)"
            class="icon-button danger"
            :title="locale === 'zh-TW' ? '刪除來源' : '删除来源'"
            :disabled="sourceAction === source.id"
            @click="removeSource(source)"
          ><Trash2 :size="16" /></button>
        </div>
      </article>
    </section>

    <section class="knowledge-stats" aria-label="Knowledge inventory">
      <div class="stat-discovered"><Globe2 :size="18" /><span>{{ locale === 'zh-TW' ? '已發現' : '已发现' }}</span><strong>{{ discoveredCount }}</strong></div>
      <div class="stat-imported"><UploadCloud :size="18" /><span>{{ locale === 'zh-TW' ? '已導入' : '已导入' }}</span><strong>{{ importedCount }}</strong></div>
      <div class="stat-published"><BookOpenText :size="18" /><span>{{ locale === 'zh-TW' ? '已發布' : '已发布' }}</span><strong>{{ publishedCount || activeCount }}</strong></div>
      <div class="draft-stat"><Clock3 :size="18" /><span>{{ locale === 'zh-TW' ? '待審核' : '待审核' }}</span><strong>{{ draftCount }}</strong></div>
      <div class="stat-failed"><CircleAlert :size="18" /><span>{{ locale === 'zh-TW' ? '失敗' : '失败' }}</span><strong>{{ failedCount }}</strong></div>
    </section>

    <section class="knowledge-quality-strip" aria-label="Content quality">
      <div class="quality-strip-heading"><ShieldCheck :size="16" /><strong>{{ locale === 'zh-TW' ? '內容品質' : '内容质量' }}</strong><span>{{ locale === 'zh-TW' ? '索引前快速檢查' : '索引前快速检查' }}</span></div>
      <div v-for="hint in qualityHints" :key="hint.key" :class="['quality-hint', `tone-${hint.tone}`]"><span>{{ hint.label }}</span><b>{{ hint.count }}</b></div>
    </section>

    <section class="knowledge-filterbar">
      <div class="search-box wide-search"><Search :size="16" /><input v-model="search" :placeholder="locale === 'zh-TW' ? '搜尋標題、正文或網址' : '搜索标题、正文或网址'" /></div>
      <select v-model="reviewFilter">
        <option value="all">{{ locale === 'zh-TW' ? '全部狀態' : '全部状态' }}</option>
        <option value="draft">{{ locale === 'zh-TW' ? '待審核' : '待审核' }}</option>
        <option value="published">{{ locale === 'zh-TW' ? '已發布' : '已发布' }}</option>
      </select>
      <select v-model="categoryFilter">
        <option value="all">{{ locale === 'zh-TW' ? '全部分類' : '全部分类' }}</option>
        <option v-for="item in categoryOptions" :key="item.value" :value="item.value">{{ locale === 'zh-TW' ? item.tw : item.cn }}</option>
      </select>
    </section>

    <section class="knowledge-list">
      <article v-for="document in filtered" :key="document.id" class="knowledge-row">
        <div class="document-icon"><Globe2 v-if="document.source_type !== 'manual'" :size="20" /><FileText v-else :size="20" /></div>
        <div class="document-copy">
          <div>
            <h3>{{ document.pending_title || document.title }}</h3>
            <span :class="['document-status', { disabled: document.review_status === 'draft' || document.pending_update }]">
              {{ document.pending_update
                ? (locale === 'zh-TW' ? '網站更新待審核' : '网站更新待审核')
                : document.review_status === 'published'
                  ? (locale === 'zh-TW' ? '已發布' : '已发布')
                  : (locale === 'zh-TW' ? '待審核' : '待审核') }}
            </span>
            <span class="document-type">{{ document.source_type.toUpperCase() }}</span>
            <span v-if="document.availability_status === 'suspected_missing'" class="document-status missing">{{ locale === 'zh-TW' ? '疑似下線' : '疑似下线' }}</span>
          </div>
          <p>{{ document.pending_content || document.content }}</p>
          <p v-if="document.pending_update" class="pending-live-note">
            {{ locale === 'zh-TW' ? '此處顯示待審核的新版本；AI 仍使用上一個已發布版本。' : '这里显示待审核的新版本；AI 仍使用上一个已发布版本。' }}
          </p>
          <footer>
            <span>{{ categoryLabel[document.category] || document.category }}</span>
            <a v-if="document.source_url" :href="document.source_url" target="_blank" rel="noreferrer">{{ document.source_url }}</a>
            <span v-else>{{ document.source }}</span>
            <span>{{ document.word_count }} {{ locale === 'zh-TW' ? '字詞' : '字词' }}</span>
            <time>{{ locale === 'zh-TW' ? '更新於' : '更新于' }} {{ formatDate(document.updated_at) }}</time>
          </footer>
        </div>
        <div class="row-actions">
          <button class="icon-button" :title="locale === 'zh-TW' ? '編輯文件' : '编辑文档'" @click="openEdit(document)"><Pencil :size="17" /></button>
          <button class="icon-button danger" :title="locale === 'zh-TW' ? '刪除文件' : '删除文档'" @click="removeDocument(document)"><Trash2 :size="17" /></button>
        </div>
      </article>
      <div v-if="!filtered.length" class="empty-state"><BookOpenText :size="28" /><span>{{ locale === 'zh-TW' ? '暫無符合條件的知識' : '暂无符合条件的知识' }}</span></div>
    </section>

    <div v-if="creating" class="modal-backdrop" @click.self="creating = false">
      <form class="modal-panel knowledge-modal" @submit.prevent="saveDocument">
        <div class="modal-heading">
          <div><p class="section-kicker">Knowledge document</p><h2>{{ editing ? (locale === 'zh-TW' ? '編輯文件' : '编辑文档') : (locale === 'zh-TW' ? '新增文件' : '添加文档') }}</h2></div>
          <button type="button" class="icon-button" :title="locale === 'zh-TW' ? '關閉' : '关闭'" @click="creating = false"><X :size="19" /></button>
        </div>
        <div class="form-grid two-columns">
          <label><span>{{ locale === 'zh-TW' ? '標題' : '标题' }}</span><input v-model="form.title" required minlength="2" /></label>
          <label><span>{{ locale === 'zh-TW' ? '分類' : '分类' }}</span><select v-model="form.category"><option v-for="item in categoryOptions" :key="item.value" :value="item.value">{{ locale === 'zh-TW' ? item.tw : item.cn }}</option></select></label>
        </div>
        <div class="form-grid two-columns">
          <label><span>{{ locale === 'zh-TW' ? '來源' : '来源' }}</span><input v-model="form.source" required :readonly="editing?.source_type !== 'manual'" /></label>
          <label v-if="editing"><span>{{ locale === 'zh-TW' ? '審核狀態' : '审核状态' }}</span><select v-model="form.review_status"><option value="draft">{{ locale === 'zh-TW' ? '待審核' : '待审核' }}</option><option value="published">{{ locale === 'zh-TW' ? '已發布，可供 AI 使用' : '已发布，可供 AI 使用' }}</option></select></label>
        </div>
        <label><span>{{ locale === 'zh-TW' ? '正文' : '正文' }}</span><textarea v-model="form.content" rows="12" required minlength="10" /></label>
        <div class="modal-actions"><button type="button" class="secondary-button" @click="creating = false">{{ locale === 'zh-TW' ? '取消' : '取消' }}</button><button class="primary-button" type="submit" :disabled="saving">{{ saving ? (locale === 'zh-TW' ? '正在儲存' : '正在保存') : (locale === 'zh-TW' ? '儲存' : '保存') }}</button></div>
      </form>
    </div>
  </div>
</template>
