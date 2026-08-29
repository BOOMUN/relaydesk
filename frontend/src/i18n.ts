import { ref } from 'vue'

// The staff workspace is intentionally Traditional Chinese only. The broader
// Locale type remains because customer content and saved reply data can still
// carry their own language tags independently from the interface language.
export type Locale = 'zh-CN' | 'zh-TW'

const messages: Record<string, string> = {
  dashboard: '概覽', inbox: '收件匣', contacts: '聯絡人', knowledge: '知識庫', productPrices: '商品價格', analytics: '分析', aiAgent: 'AI 代理', settings: '設定',
  all: '全部', mine: '我的', unassigned: '未分配', unread: '未讀', waiting: '等待中', open: '處理中', pending: '待處理', solved: '已解決',
  search: '搜尋客戶或會話', noConversations: '暫無會話', selectConversation: '選擇一個會話',
  status: '狀態', priority: '優先級', assignment: '分配', team: '團隊', agent: '客服', unassignedValue: '未分配',
  ai: 'AI 自動處理', restoreAi: '恢復 AI', pauseAi: '暫停 AI', send: '傳送', internalNote: '內部備註', reply: '回覆',
  quickReplies: '快速回覆', addNote: '新增備註', customer: '客戶', tags: '標籤', customFields: '自訂欄位', activity: '活動記錄',
  save: '儲存', cancel: '取消', addTag: '新增標籤', noTags: '暫無標籤', serviceWindow: 'WhatsApp 視窗', remaining: '剩餘',
  connected: '已連線', seats: '客服席位',
  notePlaceholder: '只對團隊可見的內部備註', replyPlaceholder: '輸入回覆內容', quickReplyPlaceholder: '選擇快速回覆',
  justNow: '剛剛', activityUpdated: '更新了會話', activitySent: '傳送了訊息', activityNote: '新增了內部備註',
}

export const locale = ref<Locale>('zh-TW')

export function useLocale() {
  const t = (key: string) => messages[key] || key
  return { locale, t }
}
