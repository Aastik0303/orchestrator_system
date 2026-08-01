import type { ElementType } from 'react'

export type NodeStatus =
  | 'idle'
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'retrying'
  | 'skipped'
  | 'waiting_for_approval'
  | 'cancelled'

export type WorkflowNodeType = 'input' | 'agent' | 'tool' | 'logic' | 'output'

export type WorkflowNodeData = {
  id: string
  label: string
  category: string
  nodeType: WorkflowNodeType
  status: NodeStatus
  lane: number
  row: number
  icon: ElementType
  model?: string
  toolName?: string
  durationMs?: number
  retryCount?: number
  inputSummary?: string
  outputSummary?: string
  error?: string
  metrics?: {
    tokens?: number
    cost?: number
    score?: number
  }
}

export type WorkflowEvent = {
  id: string
  time: string
  nodeId?: string
  message: string
  status: NodeStatus
}

export type WorkflowEdge = {
  from: string
  to: string
}

export type WorkflowRunState = {
  id: string
  status: NodeStatus
  nodes: WorkflowNodeData[]
  edges: WorkflowEdge[]
  events: WorkflowEvent[]
  reportPreview?: string
  evaluationScore?: number
}
