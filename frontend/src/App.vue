<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, ApiError } from './api'
import type { Bootstrap } from './types'
import LoginView from './components/LoginView.vue'
import AppShell from './components/AppShell.vue'

const session = ref<Bootstrap | null | undefined>(undefined)

onMounted(async () => {
  try {
    session.value = await api.get<Bootstrap>('/api/auth/me')
  } catch (error) {
    if (error instanceof ApiError && error.status !== 401) console.error(error)
    session.value = null
  }
})

async function logout() {
  await api.post('/api/auth/logout')
  session.value = null
}
</script>

<template>
  <div v-if="session === undefined" class="boot-screen">
    <div class="brand-symbol">A</div>
    <span>RelayDesk</span>
  </div>
  <LoginView v-else-if="session === null" @authenticated="session = $event" />
  <AppShell v-else :session="session" @logout="logout" />
</template>
