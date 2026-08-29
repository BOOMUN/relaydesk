<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  ArrowUpRight,
  CalendarClock,
  CircleDollarSign,
  Clock3,
  Globe2,
  History,
  Link2,
  PackageSearch,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
  X,
} from '@lucide/vue'
import { api } from '../api'
import { useLocale } from '../i18n'
import type {
  ProductPriceHistory,
  ProductPriceOffer,
  ProductPriceProduct,
  ProductPriceSource,
} from '../types'

const { locale } = useLocale()
const sources = ref<ProductPriceSource[]>([])
const products = ref<ProductPriceProduct[]>([])
const loading = ref(true)
const adding = ref(false)
const sourceAction = ref<number | null>(null)
const selectedSource = ref<number | 'all'>('all')
const search = ref('')
const category = ref('all')
const errorMessage = ref('')
const sourceForm = ref({ name: '', root_url: '', max_pages: 100 })
const historyOffer = ref<{ product: ProductPriceProduct; offer: ProductPriceOffer } | null>(null)
const history = ref<ProductPriceHistory[]>([])
const historyLoading = ref(false)
let pollTimer: number | undefined

const statusLabel = computed<Record<string, string>>(() => ({
  queued: locale.value === 'zh-TW' ? '等待同步' : '等待同步',
  running: locale.value === 'zh-TW' ? '正在同步' : '正在同步',
  completed: locale.value === 'zh-TW' ? '同步完成' : '同步完成',
  partial: locale.value === 'zh-TW' ? '部分完成' : '部分完成',
  failed: locale.value === 'zh-TW' ? '同步失敗' : '同步失败',
}))

const categoryOptions = [
  { value: 'all', cn: '全部分类', tw: '全部分類' },
  { value: 'wifi_5g', cn: '5G WiFi 蛋', tw: '5G WiFi 蛋' },
  { value: 'wifi_4g', cn: '4G WiFi 蛋', tw: '4G WiFi 蛋' },
  { value: 'esim', cn: 'eSIM 套餐', tw: 'eSIM 套餐' },
  { value: 'travel_gadget', cn: '旅行设备', tw: '旅行設備' },
  { value: 'eshop', cn: '商城商品', tw: '網店商品' },
  { value: 'other', cn: '其他商品', tw: '其他商品' },
]

const categoryLabels = computed<Record<string, string>>(() => Object.fromEntries(
  categoryOptions.map((item) => [item.value, locale.value === 'zh-TW' ? item.tw : item.cn]),
))

const hasRunningSource = computed(() => sources.value.some((item) => ['queued', 'running'].includes(item.status)))
const offerCount = computed(() => products.value.reduce((total, product) => total + product.offers.length, 0))
const changedCount = computed(() => sources.value.reduce((total, source) => total + source.last_changed_offers, 0))

const filteredProducts = computed(() => {
  const query = search.value.trim().toLocaleLowerCase()
  return products.value.filter((product) => {
    if (selectedSource.value !== 'all' && product.source_id !== selectedSource.value) return false
    if (category.value !== 'all' && product.category !== category.value) return false
    if (!query) return true
    return [
      product.name,
      product.destination || '',
      product.network || '',
      product.source_name,
      ...product.aliases,
      ...Object.values(product.name_translations),
    ].join(' ').toLocaleLowerCase().includes(query)
  })
})

const groupedProducts = computed(() => {
  const groups = new Map<number, ProductPriceProduct[]>()
  for (const product of filteredProducts.value) {
    const list = groups.get(product.source_id) || []
    list.push(product)
    groups.set(product.source_id, list)
  }
  return [...groups.entries()].map(([sourceId, items]) => ({
    source: sources.value.find((source) => source.id === sourceId),
    products: items,
  })).filter((group) => group.source)
})

async function loadAll() {
  const [sourceData, productData] = await Promise.all([
    api.get<ProductPriceSource[]>('/api/product-prices/sources'),
    api.get<ProductPriceProduct[]>('/api/product-prices/products'),
  ])
  sources.value = sourceData
  products.value = productData
}

async function addSource() {
  adding.value = true
  errorMessage.value = ''
  try {
    await api.post('/api/product-prices/sources', {
      root_url: sourceForm.value.root_url,
      name: sourceForm.value.name.trim() || null,
      max_pages: sourceForm.value.max_pages,
    })
    sourceForm.value = { name: '', root_url: '', max_pages: 100 }
    await loadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    adding.value = false
  }
}

async function syncSource(source: ProductPriceSource) {
  sourceAction.value = source.id
  errorMessage.value = ''
  try {
    await api.post(`/api/product-prices/sources/${source.id}/sync`)
    await loadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    sourceAction.value = null
  }
}

async function removeSource(source: ProductPriceSource) {
  const message = locale.value === 'zh-TW'
    ? `刪除「${source.name}」及其全部商品價格與歷史記錄？`
    : `删除“${source.name}”及其全部商品价格与历史记录？`
  if (!window.confirm(message)) return
  sourceAction.value = source.id
  errorMessage.value = ''
  try {
    await api.delete(`/api/product-prices/sources/${source.id}`)
    if (selectedSource.value === source.id) selectedSource.value = 'all'
    await loadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    sourceAction.value = null
  }
}

async function openHistory(product: ProductPriceProduct, offer: ProductPriceOffer) {
  historyOffer.value = { product, offer }
  historyLoading.value = true
  try {
    history.value = await api.get<ProductPriceHistory[]>(`/api/product-prices/offers/${offer.id}/history`)
  } finally {
    historyLoading.value = false
  }
}

function productName(product: ProductPriceProduct) {
  return product.name_translations[locale.value] || product.name
}

function offerLabel(offer: ProductPriceOffer) {
  if (locale.value === 'zh-CN' && typeof offer.metadata_json.label_zh_cn === 'string') {
    return offer.metadata_json.label_zh_cn
  }
  return offer.label
}

function money(value: string | number, currency = 'HKD') {
  const amount = Number(value)
  const prefix = currency === 'HKD' ? 'HK$' : `${currency} `
  return `${prefix}${Number.isInteger(amount) ? amount.toFixed(0) : amount.toFixed(2)}`
}

function unitLabel(unit: string) {
  if (unit === 'day') return '/日'
  return ''
}

function formatDate(value: string | null) {
  if (!value) return '—'
  return new Intl.DateTimeFormat(locale.value === 'zh-TW' ? 'zh-TW' : 'zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(value))
}

onMounted(async () => {
  try {
    await loadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    loading.value = false
  }
  pollTimer = window.setInterval(async () => {
    if (hasRunningSource.value) await loadAll()
  }, 2500)
})

onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="page-view product-price-view">
    <section class="page-toolbar product-price-toolbar">
      <div>
        <p class="section-kicker">Structured catalogue pricing</p>
        <h2>{{ locale === 'zh-TW' ? '商品價格' : '商品价格' }}</h2>
        <p class="toolbar-description">
          {{ locale === 'zh-TW'
            ? '按網址分組管理可報價商品；AI 只讀取這裡的結構化價格，不從知識片段猜測。'
            : '按网址分组管理可报价商品；AI 只读取这里的结构化价格，不从知识片段猜测。' }}
        </p>
      </div>
      <div class="schedule-chip"><CalendarClock :size="17" />{{ locale === 'zh-TW' ? '每日' : '每天' }} 03:10 · Asia/Shanghai</div>
    </section>

    <section class="price-source-create">
      <div class="ingest-heading">
        <div class="ingest-icon price"><Link2 :size="22" /></div>
        <div>
          <p class="section-kicker">Product source</p>
          <h3>{{ locale === 'zh-TW' ? '新增商品網址' : '新增商品网址' }}</h3>
          <p>{{ locale === 'zh-TW' ? '支援 SongWiFi 專用採集及標準 Schema.org Product 網站。' : '支持 SongWiFi 专用采集及标准 Schema.org Product 网站。' }}</p>
        </div>
      </div>
      <form class="price-source-form" @submit.prevent="addSource">
        <label><span>{{ locale === 'zh-TW' ? '來源名稱（可選）' : '来源名称（可选）' }}</span><input v-model="sourceForm.name" maxlength="160" :placeholder="locale === 'zh-TW' ? '例如：香港官網' : '例如：香港官网'" /></label>
        <label class="source-url-input"><span>{{ locale === 'zh-TW' ? '商品網址' : '商品网址' }}</span><input v-model="sourceForm.root_url" type="url" required placeholder="https://example.com/" /></label>
        <button class="primary-button" type="submit" :disabled="adding"><Plus :size="17" />{{ adding ? (locale === 'zh-TW' ? '正在新增' : '正在添加') : (locale === 'zh-TW' ? '新增並同步' : '添加并同步') }}</button>
      </form>
    </section>

    <p v-if="errorMessage" class="form-error"><ShieldCheck :size="16" />{{ errorMessage }}</p>

    <section class="price-stat-grid">
      <div><Globe2 :size="19" /><span>{{ locale === 'zh-TW' ? '商品網址' : '商品网址' }}</span><strong>{{ sources.length }}</strong></div>
      <div><PackageSearch :size="19" /><span>{{ locale === 'zh-TW' ? '有效商品' : '有效商品' }}</span><strong>{{ products.length }}</strong></div>
      <div><CircleDollarSign :size="19" /><span>{{ locale === 'zh-TW' ? '可報價規格' : '可报价规格' }}</span><strong>{{ offerCount }}</strong></div>
      <div><History :size="19" /><span>{{ locale === 'zh-TW' ? '最近變價' : '最近变价' }}</span><strong>{{ changedCount }}</strong></div>
    </section>

    <section class="price-source-strip">
      <button :class="{ active: selectedSource === 'all' }" @click="selectedSource = 'all'">
        {{ locale === 'zh-TW' ? '全部網址' : '全部网址' }} <span>{{ products.length }}</span>
      </button>
      <button v-for="source in sources" :key="source.id" :class="{ active: selectedSource === source.id }" @click="selectedSource = source.id">
        <Globe2 :size="15" />{{ source.name }} <span>{{ source.imported_products }}</span>
      </button>
    </section>

    <section class="price-source-cards">
      <article v-for="source in sources" :key="source.id" class="price-source-card">
        <div class="price-source-card-main">
          <div class="source-domain-icon"><Globe2 :size="20" /></div>
          <div>
            <div class="price-source-name"><strong>{{ source.name }}</strong><span :class="['crawl-status', source.status]">{{ statusLabel[source.status] }}</span></div>
            <a :href="source.root_url" target="_blank" rel="noreferrer">{{ source.root_url }} <ArrowUpRight :size="13" /></a>
            <p>{{ source.imported_products }} {{ locale === 'zh-TW' ? '個商品' : '个商品' }} · {{ source.imported_offers }} {{ locale === 'zh-TW' ? '個價格規格' : '个价格规格' }} · {{ source.adapter }}</p>
          </div>
        </div>
        <div class="price-source-sync">
          <span><Clock3 :size="14" />{{ locale === 'zh-TW' ? '上次' : '上次' }} {{ formatDate(source.completed_at) }}</span>
          <span><CalendarClock :size="14" />{{ locale === 'zh-TW' ? '下次' : '下次' }} {{ formatDate(source.next_sync_at) }}</span>
        </div>
        <p v-if="source.error_message" class="source-error">{{ source.error_message }}</p>
        <div class="price-source-actions">
          <button class="secondary-button compact-button" :disabled="['queued', 'running'].includes(source.status) || sourceAction === source.id" @click="syncSource(source)"><RefreshCw :size="15" :class="{ spinning: sourceAction === source.id || ['queued', 'running'].includes(source.status) }" />{{ locale === 'zh-TW' ? '立即同步' : '立即同步' }}</button>
          <button class="icon-button danger" :title="locale === 'zh-TW' ? '刪除來源' : '删除来源'" :disabled="['queued', 'running'].includes(source.status)" @click="removeSource(source)"><Trash2 :size="16" /></button>
        </div>
      </article>
      <div v-if="!sources.length && !loading" class="empty-state"><Globe2 :size="27" /><span>{{ locale === 'zh-TW' ? '尚未新增商品網址' : '尚未添加商品网址' }}</span></div>
    </section>

    <section class="price-filterbar">
      <div class="search-field"><Search :size="17" /><input v-model="search" :placeholder="locale === 'zh-TW' ? '搜尋目的地、商品或網址' : '搜索目的地、商品或网址'" /></div>
      <select v-model="category"><option v-for="item in categoryOptions" :key="item.value" :value="item.value">{{ locale === 'zh-TW' ? item.tw : item.cn }}</option></select>
    </section>

    <section v-for="group in groupedProducts" :key="group.source!.id" class="catalog-group">
      <header>
        <div><Globe2 :size="17" /><strong>{{ group.source!.name }}</strong><a :href="group.source!.root_url" target="_blank" rel="noreferrer">{{ group.source!.domain }}</a></div>
        <span>{{ group.products.length }} {{ locale === 'zh-TW' ? '個商品' : '个商品' }}</span>
      </header>
      <div class="price-table-wrap">
        <table class="price-table">
          <thead><tr><th>{{ locale === 'zh-TW' ? '商品' : '商品' }}</th><th>{{ locale === 'zh-TW' ? '分類' : '分类' }}</th><th>{{ locale === 'zh-TW' ? '規格' : '规格' }}</th><th>{{ locale === 'zh-TW' ? '目前價格' : '当前价格' }}</th><th>{{ locale === 'zh-TW' ? '原價' : '原价' }}</th><th>{{ locale === 'zh-TW' ? '最後同步' : '最后同步' }}</th><th /></tr></thead>
          <tbody>
            <template v-for="product in group.products" :key="product.id">
              <tr v-for="(offer, offerIndex) in product.offers" :key="offer.id">
                <td><div class="product-name-cell"><strong>{{ productName(product) }}</strong><a v-if="offerIndex === 0" :href="product.canonical_url" target="_blank" rel="noreferrer"><ArrowUpRight :size="13" /></a><span v-if="product.network">{{ product.network }}</span></div></td>
                <td><span class="category-pill">{{ categoryLabels[product.category] || product.category }}</span></td>
                <td><span class="offer-label">{{ offer.data_label ? `${offer.data_label}${offer.duration_days ? ` / ${offer.duration_days}日` : ''}` : offerLabel(offer) }}</span></td>
                <td><strong class="current-price">{{ money(offer.price_amount, offer.currency) }}{{ unitLabel(offer.unit) }}</strong><span v-if="offer.availability === 'out_of_stock'" class="stock-label out">{{ locale === 'zh-TW' ? '缺貨' : '缺货' }}</span><span v-else-if="offer.promo_label" class="promo-label">{{ locale === 'zh-TW' ? '優惠價' : '优惠价' }}</span></td>
                <td><span :class="{ 'original-price-value': offer.original_amount !== null }">{{ offer.original_amount !== null ? `${money(offer.original_amount, offer.currency)}${unitLabel(offer.unit)}` : '—' }}</span></td>
                <td><time>{{ formatDate(offer.last_seen_at) }}</time></td>
                <td><button class="icon-button" :title="locale === 'zh-TW' ? '價格歷史' : '价格历史'" @click="openHistory(product, offer)"><History :size="16" /></button></td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </section>
    <div v-if="!filteredProducts.length && sources.length && !loading" class="empty-state"><PackageSearch :size="28" /><span>{{ locale === 'zh-TW' ? '沒有符合條件的商品' : '没有符合条件的商品' }}</span></div>

    <div v-if="historyOffer" class="modal-backdrop" @click.self="historyOffer = null">
      <section class="modal-panel price-history-modal">
        <div class="modal-heading"><div><p class="section-kicker">Price history</p><h2>{{ productName(historyOffer.product) }}</h2><p>{{ offerLabel(historyOffer.offer) }}</p></div><button class="icon-button" @click="historyOffer = null"><X :size="19" /></button></div>
        <div v-if="historyLoading" class="loading-state"><RefreshCw class="spinning" :size="20" /></div>
        <div v-else class="history-list">
          <div v-for="item in history" :key="item.id"><span :class="['history-change', item.change_type]">{{ item.change_type }}</span><strong>{{ money(item.price_amount, item.currency) }}{{ unitLabel(item.unit) }}</strong><span v-if="item.original_amount !== null">{{ locale === 'zh-TW' ? '原價' : '原价' }} {{ money(item.original_amount, item.currency) }}</span><time>{{ formatDate(item.observed_at) }}</time></div>
          <div v-if="!history.length" class="empty-state">{{ locale === 'zh-TW' ? '暫無價格歷史' : '暂无价格历史' }}</div>
        </div>
      </section>
    </div>
  </div>
</template>
