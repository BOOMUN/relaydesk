<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { Search, Tag, UserRound, X } from '@lucide/vue'
import { api } from '../api'
import { useLocale } from '../i18n'
import type { Contact } from '../types'

const contacts = ref<Contact[]>([])
const { locale } = useLocale()
const search = ref('')
const selected = ref<Contact | null>(null)
const tagInput = ref('')
let searchTimer: number | undefined

async function loadContacts() {
  const params = search.value.trim() ? `?search=${encodeURIComponent(search.value.trim())}` : ''
  contacts.value = await api.get<Contact[]>(`/api/contacts${params}`)
}

async function saveContact() {
  if (!selected.value) return
  const tags = tagInput.value.split(',').map((item) => item.trim()).filter(Boolean)
  selected.value = await api.patch<Contact>(`/api/contacts/${selected.value.id}`, {
    display_name: selected.value.display_name,
    language: selected.value.language,
    tags,
  })
  tagInput.value = selected.value.tags.join(', ')
  await loadContacts()
}

function openContact(contact: Contact) {
  selected.value = { ...contact }
  tagInput.value = contact.tags.join(', ')
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat(locale.value === 'zh-TW' ? 'zh-TW' : 'zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(value))
}

watch(search, () => {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(loadContacts, 250)
})
onMounted(loadContacts)
</script>

<template>
  <div class="page-view contacts-view">
    <section class="page-toolbar">
      <div>
        <p class="section-kicker">Customer directory</p>
        <h2>{{ locale === 'zh-TW' ? '客戶資料' : '客户资料' }}</h2>
      </div>
      <div class="search-box wide-search"><Search :size="16" /><input v-model="search" :placeholder="locale === 'zh-TW' ? '搜尋姓名或手機號碼' : '搜索姓名或手机号'" /></div>
    </section>

    <section class="panel table-panel">
      <table class="data-table">
        <thead><tr><th>{{ locale === 'zh-TW' ? '客戶' : '客户' }}</th><th>WhatsApp</th><th>{{ locale === 'zh-TW' ? '最近識別語言' : '最近识别语言' }}</th><th>{{ locale === 'zh-TW' ? '標籤' : '标签' }}</th><th>{{ locale === 'zh-TW' ? '首次進入' : '首次进入' }}</th><th /></tr></thead>
        <tbody>
          <tr v-for="contact in contacts" :key="contact.id">
            <td><div class="table-person"><div class="contact-avatar">{{ contact.display_name.slice(0, 1) }}</div><strong>{{ contact.display_name }}</strong></div></td>
            <td>{{ contact.phone }}</td>
            <td>{{ contact.language }}</td>
            <td><div class="profile-tags"><span v-for="tag in contact.tags" :key="tag">{{ tag }}</span><span v-if="!contact.tags.length" class="muted-tag">{{ locale === 'zh-TW' ? '無' : '无' }}</span></div></td>
            <td>{{ formatDate(contact.created_at) }}</td>
            <td><button class="secondary-button compact-button" @click="openContact(contact)">{{ locale === 'zh-TW' ? '編輯' : '编辑' }}</button></td>
          </tr>
        </tbody>
      </table>
      <div v-if="!contacts.length" class="empty-state"><UserRound :size="28" /><span>{{ locale === 'zh-TW' ? '暫無聯絡人' : '暂无联系人' }}</span></div>
    </section>

    <div v-if="selected" class="modal-backdrop" @click.self="selected = null">
      <form class="modal-panel small-modal" @submit.prevent="saveContact">
        <div class="modal-heading">
          <div><p class="section-kicker">Contact</p><h2>{{ locale === 'zh-TW' ? '編輯客戶資料' : '编辑客户资料' }}</h2></div>
          <button type="button" class="icon-button" :title="locale === 'zh-TW' ? '關閉' : '关闭'" @click="selected = null"><X :size="19" /></button>
        </div>
        <label><span>{{ locale === 'zh-TW' ? '顯示名稱' : '显示名称' }}</span><input v-model="selected.display_name" required /></label>
        <p class="muted-copy">{{ locale === 'zh-TW' ? 'AI 會依每則客戶訊息自動識別語言；此資料僅供檢視。' : 'AI 会按每条客户消息自动识别语言；此资料仅供查看。' }}</p>
        <label><span><Tag :size="14" />{{ locale === 'zh-TW' ? '標籤' : '标签' }}</span><input v-model="tagInput" :placeholder="locale === 'zh-TW' ? '售後, 重點客戶' : '售后, 重点客户'" /></label>
        <div class="modal-actions"><button type="button" class="secondary-button" @click="selected = null">{{ locale === 'zh-TW' ? '取消' : '取消' }}</button><button class="primary-button" type="submit">{{ locale === 'zh-TW' ? '儲存' : '保存' }}</button></div>
      </form>
    </div>
  </div>
</template>
