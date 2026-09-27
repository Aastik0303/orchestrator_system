export type BackendAgentName =
  | 'auto'
  | 'supervisor'
  | 'planner'
  | 'memory'
  | 'general_chat'
  | 'deep_research'
  | 'document_rag'
  | 'youtube_rag'
  | 'code_dev'
  | 'data_analyst'
  | 'sql_agent'
  | 'python_executor'
  | 'report_generator'
  | 'evaluation'
  | (string & {})

export type RoutingDecision = {
  intent: string
  required_capabilities: string[]
  candidate_agents: string[]
  primary_agent: BackendAgentName
  secondary_agents: BackendAgentName[]
  confidence: number
  reason: string
  requires_planning: boolean
  strategy: 'override' | 'rule' | 'capability' | 'llm' | 'fallback'
}

export type RunStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'blocked' | 'timeout'

export type StructuredFailure = {
  agent: string | null
  step_id: string | null
  status: string
  error_type: string
  retryable: boolean
  message: string
  attempts: number
  details?: Record<string, unknown>
}

export type RunMetrics = {
  duration_ms: number
  tokens: number
  llm_calls: number
  tool_calls: number
  retries: number
  steps: number
  by_kind?: Record<string, { count: number; total_ms: number; max_ms: number }>
  top_spans?: Array<{ kind: string; name: string; latency_ms: number }>
}

export type RunStep = {
  step_id: string
  agent: string | null
  status: 'PENDING' | 'RUNNING' | 'SUCCESS' | 'FAILED' | 'RETRYING' | 'BLOCKED' | 'CANCELLED' | 'TIMEOUT'
  attempts: number
  depends_on: string[]
  error: StructuredFailure | null
  output_preview: string | null
  latency_ms: number | null
}

export type TraceSpan = {
  span_id: string
  parent_id: string | null
  kind: string
  name: string
  status: string
  latency_ms: number
  attributes: Record<string, unknown>
  started_at: string
}

export type LatencyContributor = {
  kind: string
  name: string
  count: number
  total_ms: number
  avg_ms: number
  p50_ms: number
  p95_ms: number
  error_rate: number
  tokens: number
}

export type EvaluationResult = {
  overall_score: number
  correctness: number
  relevance: number
  completeness: number
  groundedness: number
  hallucination_risk: 'low' | 'medium' | 'high'
  tool_success_rate: number
  format_valid: boolean
  retry_recommended: boolean
  feedback: string[]
}

export type ChatResponse = {
  active_agent: BackendAgentName
  response: string
  artifacts: Array<Record<string, unknown>>
  needs_clarification: boolean
  status: RunStatus
  route?: RoutingDecision
  evaluation?: EvaluationResult
  failures: StructuredFailure[]
  metrics: Partial<RunMetrics>
  run_id: string
  trace_id?: string
  session_id?: string
}

export type ChatRunStart = {
  run_id: string
  session_id: string
  status: 'queued'
}

export type ChatSession = {
  id: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
}

export type PersistedChatMessage = {
  id: string
  session_id: string
  role: 'user' | 'assistant'
  content: string
  attachment_name: string | null
  created_at: string
}

export type BackendWorkflowEvent = {
  sequence?: number
  id?: string
  type: string
  run_id: string
  timestamp?: string
  node_id?: string | null
  parent_node_id?: string | null
  node_type?: string | null
  label?: string | null
  status?: string | null
  details?: Record<string, unknown>
}

export type RuntimeRun = {
  id: string
  task: string
  status: RunStatus
  active_agent: BackendAgentName | null
  trace_id?: string | null
  failure?: StructuredFailure[] | null
  metrics?: Partial<RunMetrics> | null
  steps?: RunStep[]
  started_at: string | null
  completed_at?: string | null
  duration_ms: number
  file_count: number
  document_ids: string[]
  response: string
  artifacts: Array<Record<string, unknown>>
  needs_clarification: boolean
  route?: RoutingDecision | null
  plan?: Record<string, unknown> | null
  error?: string | null
  events?: BackendWorkflowEvent[]
  evaluation?: EvaluationResult | null
}

export type RuntimeDocument = {
  id: string
  name: string
  content_type: string | null
  storage_path: string | null
  size: number
  status: string
  chunk_count: number
  error?: string | null
}

export type RuntimeReport = {
  id: string
  run_id: string
  title: string
  format: string
  created_at: string | null
  status: string
  active_agent: BackendAgentName
  content: string
}

export type AgentCapability = {
  id: BackendAgentName
  name: string
  category: 'system' | 'task' | 'output'
  description: string
  status: string
  model: string
  capabilities: string[]
  tools: string[]
  timeout_seconds: number
  retry_policy: { max_retries: number; backoff_base_seconds: number; backoff_max_seconds: number }
  token_budget: number
  permissions: string[]
  memory_access: string
  user_selectable: boolean
  run_count: number
  success_rate: number | null
  average_latency_ms: number | null
}

export type McpToolCapability = {
  name: string
  qualified_name?: string
  description: string
  permission: 'allowed' | 'approval_required' | 'blocked'
  risk_level?: 'low' | 'medium' | 'high' | 'critical'
  required_permissions?: string[]
  timeout_seconds?: number
  requires_approval?: boolean
  status: string
  average_latency_ms: number | null
  success_rate: number | null
  last_used: string | null
}

export type McpServerCapability = {
  id: string
  name: string
  status: string
  tools: McpToolCapability[]
}

export type RuntimeSnapshot = {
  health: string
  runs: RuntimeRun[]
  documents: RuntimeDocument[]
  reports: RuntimeReport[]
  evaluation: {
    available: boolean
    successful_runs: number
    failed_runs: number
    blocked_runs?: number
    success_rate: number
    average_latency_ms: number
    average_quality_score: number | null
  }
}

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? (import.meta.env.PROD ? '' : 'http://127.0.0.1:8002')
const WS_BASE_URL = API_BASE_URL
  ? API_BASE_URL.replace(/^http/, 'ws')
  : window.location.origin.replace(/^http/, 'ws')

const API_KEY = import.meta.env.VITE_API_KEY as string | undefined

function authHeaders(extra?: HeadersInit): HeadersInit {
  const headers = new Headers(extra)
  if (API_KEY) headers.set('Authorization', `Bearer ${API_KEY}`)
  return headers
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers: authHeaders(init?.headers) })
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `Backend request failed: ${response.status}`)
  }
  return response.json() as Promise<T>
}

/** Save a file an agent wrote (e.g. a transformed dataset). Fetched with the
 * caller's credentials, so it cannot be a plain link. */
export async function downloadArtifact(path: string, fileName: string) {
  const encoded = path.split('/').map(encodeURIComponent).join('/')
  const response = await fetch(`${API_BASE_URL}/api/artifacts/${encoded}`, { headers: authHeaders() })
  if (!response.ok) throw new Error(response.status === 404 ? 'File is no longer available.' : `Download failed: ${response.status}`)
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = fileName
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function getBackendHealth() {
  return requestJson<{ status: string; database: string; mcp_servers: number }>('/health')
}

type ChatInput = {
  message: string
  files: File[]
  // Documents already uploaded via uploadKnowledgeDocuments, attached by id.
  documentIds?: string[]
  sessionId?: string
}

function chatFormData({ message, files, documentIds, sessionId }: ChatInput) {
  const formData = new FormData()
  formData.set('message', message)
  formData.set('agent_override', 'auto')
  formData.set('deep_research', 'false')
  if (sessionId) formData.set('session_id', sessionId)
  if (documentIds?.length) formData.set('document_ids', documentIds.join(','))
  files.forEach(file => formData.append('files', file))
  return formData
}

export function sendChatMessage(input: ChatInput) {
  return requestJson<ChatResponse>('/api/chat', { method: 'POST', body: chatFormData(input) })
}

export function startChatRun(input: ChatInput) {
  return requestJson<ChatRunStart>('/api/chat/start', { method: 'POST', body: chatFormData(input) })
}

export function subscribeToRun(
  runId: string,
  onEvent: (event: BackendWorkflowEvent) => void,
  onClose: () => void,
  onError: () => void,
) {
  // Browsers cannot set headers on WebSocket upgrades; the key goes in the query.
  const query = API_KEY ? `?api_key=${encodeURIComponent(API_KEY)}` : ''
  const socket = new WebSocket(`${WS_BASE_URL}/ws/runs/${encodeURIComponent(runId)}${query}`)
  socket.onmessage = message => onEvent(JSON.parse(message.data) as BackendWorkflowEvent)
  socket.onclose = onClose
  socket.onerror = onError
  return socket
}

export function getRun(runId: string) {
  return requestJson<RuntimeRun>(`/api/runs/${encodeURIComponent(runId)}`)
}

export async function uploadKnowledgeDocuments(files: File[]) {
  const formData = new FormData()
  files.forEach(file => formData.append('files', file))
  return requestJson<{ documents: RuntimeDocument[] }>('/api/documents/upload', { method: 'POST', body: formData })
}

export function reindexKnowledgeDocument(documentId: string) {
  return requestJson<RuntimeDocument>(`/api/documents/${encodeURIComponent(documentId)}/reindex`, { method: 'POST' })
}

export function deleteKnowledgeDocument(documentId: string) {
  return requestJson<{ deleted: boolean; document_id: string }>(`/api/documents/${encodeURIComponent(documentId)}`, { method: 'DELETE' })
}

export function getRuntimeSnapshot() {
  return requestJson<RuntimeSnapshot>('/api/runtime')
}

export function getAgentCapabilities() {
  return requestJson<{ agents: AgentCapability[] }>('/api/capabilities/agents')
}

export function getMcpCapabilities() {
  return requestJson<{ servers: McpServerCapability[] }>('/api/capabilities/mcp')
}

export function testMcpConnection(serverId: string) {
  return requestJson<{ id: string; status: string; tested_at: string }>(`/api/capabilities/mcp/${encodeURIComponent(serverId)}/test`, { method: 'POST' })
}

export function getChatSessions() {
  return requestJson<{ sessions: ChatSession[] }>('/api/chat/sessions')
}

export function createChatSession() {
  return requestJson<{ session: ChatSession }>('/api/chat/sessions', { method: 'POST' })
}

export function getChatMessages(sessionId: string) {
  return requestJson<{ messages: PersistedChatMessage[] }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages`)
}

export function deleteChatSession(sessionId: string) {
  return requestJson<{ deleted: boolean; session_id: string }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
}

export function stopRun(runId: string) {
  return requestJson<{ run_id: string; status: string }>(`/api/runs/${encodeURIComponent(runId)}/stop`, { method: 'POST' })
}

export function getRunTrace(runId: string) {
  return requestJson<{ run_id: string; trace_id: string | null; metrics: Partial<RunMetrics> | null; spans: TraceSpan[] }>(
    `/api/runs/${encodeURIComponent(runId)}/trace`,
  )
}

export function getLatencyMetrics() {
  return requestJson<{ spans_analyzed: number; top_contributors: LatencyContributor[] }>('/api/metrics/latency')
}

export type EvalSuiteReport = {
  recorded_at: string
  passed: boolean
  failures: string[]
  summary: Record<string, Record<string, unknown>>
}

export function getLatestEvalSuite() {
  return requestJson<EvalSuiteReport>('/api/evaluations/suites/latest')
}
