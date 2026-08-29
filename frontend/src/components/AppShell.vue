<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  BarChart3,
  BookOpenText,
  Bot,
  CircleDollarSign,
  Inbox,
  LayoutDashboard,
  LogOut,
  Settings,
  Users,
} from '@lucide/vue'
import type { Bootstrap } from '../types'
import { useLocale } from '../i18n'
import DashboardView from './DashboardView.vue'
import InboxView from './InboxView.vue'
import ContactsView from './ContactsView.vue'
import KnowledgeView from './KnowledgeView.vue'
import ProductPricesView from './ProductPricesView.vue'
import AnalyticsView from './AnalyticsView.vue'
import AiAgentView from './AiAgentView.vue'
import SettingsView from './SettingsView.vue'

const props = defineProps<{ session: Bootstrap }>()
const emit = defineEmits<{ logout: [] }>()
const active = ref('inbox')
const { locale, t } = useLocale()

const items = computed(() => [
  { id: 'dashboard', label: t('dashboard'), icon: LayoutDashboard },
  { id: 'inbox', label: t('inbox'), icon: Inbox },
  { id: 'contacts', label: t('contacts'), icon: Users },
  { id: 'knowledge', label: t('knowledge'), icon: BookOpenText },
  { id: 'product-prices', label: t('productPrices'), icon: CircleDollarSign },
  { id: 'analytics', label: t('analytics'), icon: BarChart3 },
  { id: 'ai-agent', label: t('aiAgent'), icon: Bot },
  { id: 'settings', label: t('settings'), icon: Settings },
])

const currentTitle = computed(() => items.value.find((item) => item.id === active.value)?.label || '')
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="sidebar-brand">
        <span class="brand-symbol">A</span>
        <div>
          <strong>{{ session.app_name }}</strong>
          <span>Customer Operations</span>
        </div>
      </div>
      <nav class="sidebar-nav" aria-label="主導覽">
        <button
          v-for="item in items"
          :key="item.id"
          class="nav-button"
          :class="{ active: active === item.id }"
          :aria-current="active === item.id ? 'page' : undefined"
          :title="item.label"
          @click="active = item.id"
        >
          <component :is="item.icon" :size="19" />
          <span>{{ item.label }}</span>
        </button>
      </nav>
      <div class="sidebar-agent">
        <div class="agent-avatar">{{ session.user.name.slice(0, 1) }}</div>
        <div class="agent-identity">
          <strong>{{ session.user.name }}</strong>
          <span>{{ session.user.role }}</span>
        </div>
        <button class="icon-button dark" title="登出" @click="emit('logout')">
          <LogOut :size="18" />
        </button>
      </div>
    </aside>

    <section class="workspace">
      <header class="workspace-header">
        <div>
          <p class="workspace-kicker">RelayDesk Workspace</p>
          <h1>{{ currentTitle }}</h1>
        </div>
        <div class="integration-indicators">
          <span :class="['status-dot-label', session.integration.whatsapp ? 'online' : 'demo']">
            <span class="status-dot" />WhatsApp {{ session.integration.whatsapp ? (locale === 'zh-TW' ? '已設定' : '已配置') : (locale === 'zh-TW' ? '示範模式' : '演示模式') }}
          </span>
          <span :class="['status-dot-label', session.integration.openai ? 'online' : 'demo']">
            <Bot :size="15" />GPT {{ session.integration.openai ? t('connected') : (locale === 'zh-TW' ? '本機模式' : '本地模式') }}
          </span>
        </div>
      </header>

      <div class="workspace-content" :class="{ 'inbox-workspace': active === 'inbox' }">
        <DashboardView v-if="active === 'dashboard'" @open-inbox="active = 'inbox'" />
        <InboxView v-else-if="active === 'inbox'" :session="props.session" />
        <ContactsView v-else-if="active === 'contacts'" />
        <KnowledgeView v-else-if="active === 'knowledge'" />
        <ProductPricesView v-else-if="active === 'product-prices'" />
        <AnalyticsView v-else-if="active === 'analytics'" />
        <AiAgentView v-else-if="active === 'ai-agent'" :session="props.session" />
        <SettingsView v-else :session="props.session" />
      </div>
    </section>

    <nav class="mobile-nav" aria-label="行動導覽">
      <button
        v-for="item in items"
        :key="item.id"
        :class="{ active: active === item.id }"
        :aria-current="active === item.id ? 'page' : undefined"
        :title="item.label"
        @click="active = item.id"
      >
        <component :is="item.icon" :size="20" />
        <span>{{ item.label }}</span>
      </button>
    </nav>
  </div>
</template>
