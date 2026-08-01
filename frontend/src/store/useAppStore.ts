import { useSyncExternalStore } from 'react'

type Project = {
  id: string
  name: string
  description: string
  color: string
  status: 'active' | 'paused' | 'archived'
  fileCount: number
  workflowRuns: number
  reports: number
  lastActivity: string
  repositoryConnected: boolean
}

type KnowledgeBase = {
  id: string
  name: string
  description: string
  status: 'ready' | 'indexing' | 'failed'
  documentCount: number
  chunkCount: number
}

type UploadedDocument = {
  id: string
  name: string
  type: string
  size: number
  status: 'uploaded' | 'indexing' | 'indexed'
  knowledgeBaseId: string
  uploadedAt: string
  chunkCount: number
}

type NewDocument = {
  name: string
  type: string
  size: number
  lastModified: number
}

type ToolConnection = {
  id: string
  name: string
  icon: string
  status: 'connected' | 'disconnected'
  lastUsed: string
  actions: Array<{ name: string; permission: 'allowed' | 'approval_required' | 'blocked' }>
}

type Settings = {
  enableLongTermMemory: boolean
  rememberProjectPreferences: boolean
  rememberWorkflowHistory: boolean
}

type AppState = {
  leftSidebarOpen: boolean
  rightSidebarOpen: boolean
  darkMode: boolean
  projects: Project[]
  knowledgeBases: KnowledgeBase[]
  uploadedDocuments: UploadedDocument[]
  toolConnections: ToolConnection[]
  settings: Settings
  toggleLeftSidebar: () => void
  toggleRightSidebar: () => void
  toggleDarkMode: () => void
  addKnowledgeDocuments: (documents: NewDocument[]) => UploadedDocument[]
  updateSettings: (settings: Partial<Settings>) => void
}

let state: AppState
const listeners = new Set<() => void>()

function emit() {
  listeners.forEach(listener => listener())
}

function setState(nextState: Partial<AppState>) {
  state = { ...state, ...nextState }
  emit()
}

state = {
  leftSidebarOpen: true,
  rightSidebarOpen: true,
  darkMode: false,
  projects: [],
  knowledgeBases: [],
  uploadedDocuments: [],
  toolConnections: [],
  settings: {
    enableLongTermMemory: true,
    rememberProjectPreferences: true,
    rememberWorkflowHistory: false,
  },
  toggleLeftSidebar: () => setState({ leftSidebarOpen: !state.leftSidebarOpen }),
  toggleRightSidebar: () => setState({ rightSidebarOpen: !state.rightSidebarOpen }),
  toggleDarkMode: () => setState({ darkMode: !state.darkMode }),
  addKnowledgeDocuments: documents => {
    const uploadedAt = new Date().toLocaleString()
    const newDocuments = documents.map((document, index) => ({
      id: `doc_${document.lastModified}_${index}_${document.name.replace(/[^a-z0-9]/gi, '_').toLowerCase()}`,
      name: document.name,
      type: document.type || 'Unknown',
      size: document.size,
      status: 'indexed' as const,
      knowledgeBaseId: 'kb_docs',
      uploadedAt,
      chunkCount: Math.max(1, Math.ceil(document.size / 1800)),
    }))

    const addedChunks = newDocuments.reduce((total, document) => total + document.chunkCount, 0)

    setState({
      uploadedDocuments: [...newDocuments, ...state.uploadedDocuments],
      knowledgeBases: state.knowledgeBases.map(knowledgeBase => knowledgeBase.id === 'kb_docs'
        ? {
            ...knowledgeBase,
            documentCount: knowledgeBase.documentCount + newDocuments.length,
            chunkCount: knowledgeBase.chunkCount + addedChunks,
          }
        : knowledgeBase
      ),
    })

    return newDocuments
  },
  updateSettings: settingsUpdate => setState({ settings: { ...state.settings, ...settingsUpdate } }),
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function getSnapshot() {
  return state
}

export function useAppStore() {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}
