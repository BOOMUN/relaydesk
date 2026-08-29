export interface User {
  id: number
  tenant_id: number
  name: string
  email: string
  role: string
}

export interface IntegrationStatus {
  openai: boolean
  whatsapp: boolean
  whatsapp_provider: 'demo' | 'meta' | 'evolution'
  mode: 'demo' | 'live'
}

export interface Bootstrap {
  app_name: string
  user: User
  integration: IntegrationStatus
}

export interface WhatsAppConnection {
  provider: 'demo' | 'meta' | 'evolution'
  configured: boolean
  state: string
  instance_name: string | null
  webhook_url: string | null
  qr_code: string | null
  message: string | null
}

export interface Contact {
  id: number
  wa_id: string
  phone: string
  display_name: string
  language: string
  tags: string[]
  custom_attributes: Record<string, unknown>
  is_blocked: boolean
  created_at: string
  updated_at: string
}

export interface Message {
  id: number
  external_id: string | null
  direction: 'inbound' | 'outbound' | 'internal'
  sender_type: 'customer' | 'ai' | 'agent' | 'system'
  sender_name: string | null
  content_type: string
  body: string
  delivery_status: string
  metadata_json: {
    route?: string
    sources?: Array<{
      document_id?: number
      product_id?: number
      title: string
      source: string
      source_url?: string
      page_title?: string
      section_path?: string
      source_updated_at?: string
      retrieval_score?: number | string | null
    }>
    language?: string
    agent_profile_version_id?: number | null
    action_execution_id?: string
    action_error_code?: string | null
    action_failure_reason?: string | null
  }
  created_at: string
}

export interface ChannelAccount {
  id: number
  provider: 'demo' | 'meta' | 'evolution'
  name: string
  external_account_id: string
  phone_number_id: string | null
  business_account_id: string | null
  instance_name: string | null
  capabilities: string[]
  is_default: boolean
  is_active: boolean
  connection_state: string
  last_checked_at: string | null
}

export interface WhatsAppTemplate {
  id: number
  channel_account_id: number
  provider_template_id: string | null
  name: string
  language: string
  category: string
  status: string
  parameter_format: string
  components: Array<Record<string, unknown>>
  quality_rating: string | null
  rejection_reason: string | null
  is_active: boolean
  last_synced_at: string
}

export interface WhatsAppTemplateSyncRun {
  id: number
  channel_account_id: number
  status: 'running' | 'completed' | 'failed'
  template_count: number
  approved_count: number
  failure_reason: string | null
  started_at: string
  completed_at: string | null
}

export interface ActionExecution {
  id: string
  tenant_id: number
  conversation_id: number | null
  contact_id: number | null
  source_message_id: number | null
  action_name: string
  purpose: string
  input_json: Record<string, unknown>
  requested_by_type: 'user' | 'system' | 'model'
  risk_level: 'low' | 'medium' | 'high'
  status: 'proposed' | 'pending_confirmation' | 'running' | 'succeeded' | 'failed' | 'rejected'
  requires_confirmation: boolean
  confirmation_reason: string | null
  idempotency_key: string
  attempt_count: number
  max_attempts: number
  result_json: Record<string, unknown>
  error_code: string | null
  failure_reason: string | null
  requires_identity_verification: boolean
  identity_verified: boolean
  created_at: string
  updated_at: string
}

export interface MessageTranslation {
  message_id: number
  source_language: 'en'
  target_language: 'zh-TW'
  translated_text: string
}

export interface Conversation {
  id: number
  contact: Contact
  status: 'open' | 'pending' | 'expired' | 'solved' | 'blocked'
  priority: 'low' | 'normal' | 'high' | 'urgent'
  subject: string
  assigned_team_id: number | null
  assigned_user_id: number | null
  assigned_team: string | null
  assigned_user: string | null
  ai_enabled: boolean
  ai_route: string | null
  unread_count: number
  last_message: string
  last_message_sender: string | null
  last_message_at: string
  service_window_expires_at: string | null
  messages?: Message[]
}

export interface Team {
  id: number
  name: string
  description: string
  is_default: boolean
}

export interface Agent {
  id: number
  name: string
  email: string
  role: string
}

export interface QuickReply {
  id: number
  shortcut: string
  title: string
  body: string
  language: string
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface AgentProfileVersion {
  id: number
  version_number: number
  status: 'draft' | 'published' | 'superseded'
  identity: string
  service_scope: string[]
  tone: string
  knowledge_priority: string[]
  prohibitions: string[]
  handoff_conditions: string[]
  reply_language: 'auto'
  fallback_language: 'zh-CN' | 'zh-TW' | 'en'
  order_intake_enabled: boolean
  automation_timeout_minutes: number
  web_search_enabled: boolean
  web_search_allowed_domains: string[]
  lead_qualification: LeadQualificationConfiguration
  instructions: string
  source_url: string | null
  generation_summary: string | null
  created_by_user_id: number | null
  published_by_user_id: number | null
  rollback_from_version_id: number | null
  created_at: string
  updated_at: string
  published_at: string | null
}

export interface LeadQualificationOption {
  value: string
  label: string
  score: number
}

export interface LeadQualificationQuestion {
  id: string
  prompt: string
  prompt_en: string | null
  kind: 'text' | 'single_choice' | 'number'
  required: boolean
  default_score: number
  options: LeadQualificationOption[]
}

export interface LeadQualificationGrade {
  name: string
  min_score: number
  tag: string | null
  priority: 'low' | 'normal' | 'high' | 'urgent' | null
  team_id: number | null
  user_id: number | null
}

export interface LeadQualificationConfiguration {
  enabled: boolean
  trigger_terms: string[]
  questions: LeadQualificationQuestion[]
  grades: LeadQualificationGrade[]
}

export interface RestActionEndpoint {
  id: number
  name: string
  description: string
  base_url: string
  path_pattern: string
  allowed_methods: Array<'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'>
  timeout_seconds: number
  requires_identity_verification: boolean
  secret_header_name: string | null
  secret_fingerprint: string | null
  has_secret: boolean
  status: 'draft' | 'approved' | 'disabled'
  created_by_user_id: number
  approved_by_user_id: number | null
  approved_at: string | null
  created_at: string
  updated_at: string
}

export interface IdentityVerification {
  id: string
  conversation_id: number
  contact_id: number
  status: 'verified' | 'revoked' | 'expired'
  method: string
  evidence_hint: string
  verified_by_user_id: number
  expires_at: string
  created_at: string
}

export interface AutomationFormSession {
  id: string
  conversation_id: number
  contact_id: number
  workflow_key: 'order_intake' | 'lead_qualification'
  operation: string
  status: 'active' | 'paused' | 'completed' | 'timed_out' | 'handed_off'
  current_step: number
  definition_json: {
    fields?: Array<{
      key?: string
      prompt?: string
      prompt_en?: string | null
      [key: string]: unknown
    }>
    [key: string]: unknown
  }
  answers_json: Record<string, unknown>
  score: number | null
  grade: string | null
  expires_at: string
  paused_at: string | null
  completed_at: string | null
  created_at: string
  updated_at: string
}

export interface AgentProfile {
  profile_id: number
  active_version: AgentProfileVersion | null
  draft_version: AgentProfileVersion | null
}

export interface ConversationActivity {
  id: number
  action: string
  user_name: string | null
  details: Record<string, unknown>
  created_at: string
}

export interface InboxStats {
  all: number
  open: number
  pending: number
  solved: number
  unread: number
  unassigned: number
  mine: number
}

export interface WorkspaceInfo {
  max_agent_seats: number
  active_agents: number
  supported_locales: string[]
  default_locale: 'zh-CN' | 'zh-TW'
}

export interface KnowledgeDocument {
  id: number
  title: string
  content: string
  source: string
  category: string
  is_active: boolean
  source_id: number | null
  source_type: 'manual' | 'html' | 'pdf'
  source_url: string | null
  review_status: 'draft' | 'published'
  language: string
  word_count: number
  pending_update: boolean
  pending_revision_id: number | null
  pending_title: string | null
  pending_content: string | null
  pending_category: string | null
  availability_status: 'active' | 'missing_once' | 'suspected_missing'
  consecutive_missing: number
  created_at: string
  updated_at: string
}

export interface KnowledgeSource {
  id: number
  root_url: string
  domain: string
  status: 'queued' | 'running' | 'completed' | 'partial' | 'failed'
  max_pages: number
  max_depth: number
  discovered_pages: number
  imported_pages: number
  failed_pages: number
  draft_pages: number
  published_pages: number
  pending_updates: number
  suspected_removed_pages: number
  error_message: string | null
  auto_sync_enabled: boolean
  sync_time: string
  sync_timezone: string
  next_sync_at: string
  next_retry_at: string | null
  last_sync_trigger: 'initial' | 'manual' | 'scheduled' | 'retry' | null
  last_new_pages: number
  last_changed_pages: number
  last_unchanged_pages: number
  last_missing_pages: number
  last_successful_sync_at: string | null
  failed_task_count: number
  partial_task_count: number
  last_failed_task_at: string | null
  last_failure_message: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  updated_at: string
}

export interface ProductPriceSource {
  id: number
  name: string
  root_url: string
  domain: string
  adapter: string
  status: 'queued' | 'running' | 'completed' | 'partial' | 'failed'
  auto_sync_enabled: boolean
  max_pages: number
  discovered_products: number
  imported_products: number
  imported_offers: number
  failed_pages: number
  error_message: string | null
  sync_time: string
  sync_timezone: string
  next_sync_at: string
  next_retry_at: string | null
  last_sync_trigger: 'initial' | 'manual' | 'scheduled' | 'retry' | null
  last_new_products: number
  last_new_offers: number
  last_changed_offers: number
  last_unchanged_offers: number
  created_at: string
  started_at: string | null
  completed_at: string | null
  updated_at: string
}

export interface ProductPriceOffer {
  id: number
  external_key: string
  label: string
  currency: string
  price_amount: string | number
  original_amount: string | number | null
  unit: string
  duration_days: number | null
  data_label: string | null
  promo_label: string | null
  availability: string
  metadata_json: Record<string, unknown>
  is_active: boolean
  last_seen_at: string
  updated_at: string
}

export interface ProductPriceProduct {
  id: number
  source_id: number
  source_name: string
  source_url: string
  canonical_url: string
  name: string
  name_translations: Record<string, string>
  aliases: string[]
  category: string
  product_type: string
  destination: string | null
  network: string | null
  description: string
  metadata_json: Record<string, unknown>
  is_active: boolean
  last_seen_at: string
  updated_at: string
  offers: ProductPriceOffer[]
}

export interface ProductPriceHistory {
  id: number
  change_type: string
  currency: string
  price_amount: string | number
  original_amount: string | number | null
  unit: string
  observed_at: string
}

export interface DashboardMetric {
  label: string
  value: number
  change: number | null
  unit: string | null
}

export interface DashboardData {
  metrics: DashboardMetric[]
  status_counts: Record<string, number>
  route_counts: Record<string, number>
  recent_conversations: Conversation[]
}

export interface QualityEvaluationReport {
  suite_version: string
  generated_at: string
  tenant_id: number
  live_model: boolean
  embedding_provider: string
  metrics: {
    intent_accuracy_pct: number
    country_recall_pct: number
    product_recall_pct: number
    retrieval_top1_accuracy_pct: number
    retrieval_top3_accuracy_pct: number
  }
  intent_accuracy: { correct_count: number; case_count: number }
  country_recall: { hit_count: number; expected_count: number; evaluated_cases: number }
  product_recall: { hit_count: number; expected_count: number; evaluated_cases: number }
  retrieval_accuracy: {
    top1_correct_count: number
    top3_correct_count: number
    evaluated_cases: number
  }
}
