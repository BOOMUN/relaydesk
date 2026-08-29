<script setup lang="ts">
import { ref } from 'vue'
import { ArrowRight, CheckCircle2, LockKeyhole, MessageSquareText, Users } from '@lucide/vue'
import { api } from '../api'
import { useLocale } from '../i18n'
import type { Bootstrap } from '../types'

const emit = defineEmits<{ authenticated: [session: Bootstrap] }>()
const { locale } = useLocale()
// Credentials must be entered by the operator; never ship account values in
// the client bundle or pre-fill them on the login screen.
const email = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')

async function login() {
  loading.value = true
  error.value = ''
  try {
    const session = await api.post<Bootstrap>('/api/auth/login', {
      email: email.value,
      password: password.value,
    })
    emit('authenticated', session)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '登入失敗'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-brand">
      <div class="login-brand-name">
        <span class="brand-symbol brand-symbol-large">A</span>
        <span>RelayDesk</span>
      </div>
      <div class="login-copy">
        <p class="eyebrow">WhatsApp Customer Operations</p>
        <h1>{{ locale === 'zh-TW' ? '讓 AI 與人工客服在同一個工作台協作' : '让 AI 与人工客服在同一个工作台协作' }}</h1>
        <div class="login-capabilities">
          <span><MessageSquareText :size="18" />{{ locale === 'zh-TW' ? '共享會話收件匣' : '共享会话收件箱' }}</span>
          <span><CheckCircle2 :size="18" />{{ locale === 'zh-TW' ? '可追蹤的 Agent 決策' : '可追踪的 Agent 决策' }}</span>
          <span><Users :size="18" />{{ locale === 'zh-TW' ? '明確的人工接管邊界' : '明确的人工接管边界' }}</span>
        </div>
      </div>
      <p class="login-version">Local framework · v0.1</p>
    </section>

    <section class="login-form-section">
      <form class="login-form" @submit.prevent="login">
        <div class="login-form-heading">
          <LockKeyhole :size="22" />
          <div>
            <h2>{{ locale === 'zh-TW' ? '登入客服工作台' : '登录客服工作台' }}</h2>
            <p>{{ locale === 'zh-TW' ? '使用團隊帳號繼續' : '使用团队账号继续' }}</p>
          </div>
        </div>
        <label>
          <span>{{ locale === 'zh-TW' ? '電子郵件' : '邮箱' }}</span>
          <input v-model="email" type="email" autocomplete="username" required />
        </label>
        <label>
          <span>{{ locale === 'zh-TW' ? '密碼' : '密码' }}</span>
          <input v-model="password" type="password" autocomplete="current-password" required />
        </label>
        <p v-if="error" class="form-error">{{ error }}</p>
        <button class="primary-button login-button" type="submit" :disabled="loading">
          <span>{{ loading ? (locale === 'zh-TW' ? '正在登入' : '正在登录') : (locale === 'zh-TW' ? '進入工作台' : '进入工作台') }}</span>
          <ArrowRight :size="17" />
        </button>
      </form>
    </section>
  </main>
</template>
