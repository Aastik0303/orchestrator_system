export type BackendAgentName =
  | 'auto'
  | 'general_chat'
  | 'deep_research'
  | 'document_rag'
  | 'youtube_rag'
  | 'code_dev'
  | 'data_analyst'

export type ChatResponse = {
  active_agent: BackendAgentName
  response: string
  artifacts: Array<Record<string, unknown>>
  needs_clarification: boolean
  session_id?: string
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

export type RuntimeRun = {
  id: string
  task: string
  status: 'completed' | 'failed' | 'running'
  active_agent: BackendAgentName
  started_at: string | null
  duration_ms: number
  file_count: number
  document_ids: string[]
  response: string
  artifacts: Array<Record<string, unknown>>
  needs_clarification: boolean
}

export type RuntimeDocument = {
  id: string
  name: string
  content_type: string | null
  storage_path: string | null
  size: number
  status: string
  chunk_count: number
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

export type RuntimeSnapshot = {
  health: string
  runs: RuntimeRun[]
  documents: RuntimeDocument[]
  reports: RuntimeReport[]
  evaluation: {
    available: boolean
    successful_runs: number
    failed_runs: number
    success_rate: number
    average_latency_ms: number
  }
  capabilities: Array<{
    id: BackendAgentName
    name: string
    status: string
  }>
}

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8002'

export async function getBackendHealth() {
  const response = await fetch(`${API_BASE_URL}/health`)
  if (!response.ok) {
    throw new Error(`Backend health check failed: ${response.status}`)
  }
  return response.json() as Promise<{ status: string }>
}

export async function sendChatMessage({
  message,
  files,
  sessionId,
}: {
  message: string
  files: File[]
  sessionId?: string
}) {
  const formData = new FormData()
  formData.set('message', message)
  formData.set('agent_override', 'auto')
  formData.set('deep_research', 'false')
  if (sessionId) formData.set('session_id', sessionId)
  files.forEach(file => formData.append('files', file))

  const response = await fetch(`${API_BASE_URL}/api/chat`, {
    method: 'POST',
    body: formData,
  })

  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `Chat request failed: ${response.status}`)
  }

  return response.json() as Promise<ChatResponse>
}

export async function uploadKnowledgeDocuments(files: File[]) {
  const formData = new FormData()
  files.forEach(file => formData.append('files', file))

  const response = await fetch(`${API_BASE_URL}/api/documents/upload`, {
    method: 'POST',
    body: formData,
  })

  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `Document upload failed: ${response.status}`)
  }

  return response.json() as Promise<{ documents: RuntimeDocument[] }>
}

export async function getRuntimeSnapshot() {
  const response = await fetch(`${API_BASE_URL}/api/runtime`)
  if (!response.ok) {
    throw new Error(`Runtime snapshot failed: ${response.status}`)
  }
  return response.json() as Promise<RuntimeSnapshot>
}

export async function getChatSessions() {
  const response = await fetch(`${API_BASE_URL}/api/chat/sessions`)
  if (!response.ok) {
    throw new Error(`Chat sessions failed: ${response.status}`)
  }
  return response.json() as Promise<{ sessions: ChatSession[] }>
}

export async function createChatSession() {
  const response = await fetch(`${API_BASE_URL}/api/chat/sessions`, {
    method: 'POST',
  })
  if (!response.ok) {
    throw new Error(`Create chat failed: ${response.status}`)
  }
  return response.json() as Promise<{ session: ChatSession }>
}

export async function getChatMessages(sessionId: string) {
  const response = await fetch(`${API_BASE_URL}/api/chat/sessions/${sessionId}/messages`)
  if (!response.ok) {
    throw new Error(`Chat messages failed: ${response.status}`)
  }
  return response.json() as Promise<{ messages: PersistedChatMessage[] }>
}
