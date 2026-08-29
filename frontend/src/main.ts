import { createApp } from 'vue'
import App from './App.vue'
import { unlockHandoffAudio } from './handoff-audio'
import './styles.css'

async function unlockNotificationSound() {
  if (!(await unlockHandoffAudio())) return
  window.removeEventListener('pointerdown', unlockNotificationSound)
  window.removeEventListener('keydown', unlockNotificationSound)
}

window.addEventListener('pointerdown', unlockNotificationSound)
window.addEventListener('keydown', unlockNotificationSound)

createApp(App).mount('#app')
