import { Bot, CheckCircle, Database, FileText, GitBranch, Globe, MemoryStick, Search, ShieldCheck, User } from 'lucide-react'
import type { NodeStatus, WorkflowEdge, WorkflowEvent, WorkflowNodeData, WorkflowRunState } from '@/types/workflow'

type ScheduledWorkflowEvent =
  | { type: 'event'; delay: number; message: string; status: NodeStatus; nodeId?: string }
  | { type: 'node'; delay: number; node: WorkflowNodeData }
  | { type: 'status'; delay: number; nodeId: string; status: NodeStatus; durationMs?: number; outputSummary?: string; retryCount?: number; metrics?: WorkflowNodeData['metrics'] }
  | { type: 'edge'; delay: number; edge: WorkflowEdge }
  | { type: 'complete'; delay: number; reportPreview: string; evaluationScore: number }

export function createInitialWorkflowRun(task: string): WorkflowRunState {
  const inputNode = makeNode({
    id: 'input',
    label: 'User Input',
    category: 'Input',
    nodeType: 'input',
    status: 'completed',
    lane: 1,
    row: 1,
    icon: User,
    outputSummary: task,
  })

  return {
    id: `run_${Date.now()}`,
    status: 'running',
    nodes: [inputNode],
    edges: [],
    events: [makeEvent('Workflow run created from chat task.', 'running', 'input')],
  }
}

export function getDemoWorkflowSchedule(task: string): ScheduledWorkflowEvent[] {
  const nodes: WorkflowNodeData[] = [
    makeNode({ id: 'supervisor', label: 'Supervisor Agent', category: 'Orchestration', nodeType: 'agent', status: 'queued', lane: 1, row: 2, icon: ShieldCheck, model: 'gpt-5-mini', inputSummary: task }),
    makeNode({ id: 'memory', label: 'Memory Agent', category: 'Memory', nodeType: 'agent', status: 'queued', lane: 1, row: 3, icon: MemoryStick, model: 'memory-router', outputSummary: '4 memories retrieved' }),
    makeNode({ id: 'planner', label: 'Planner Agent', category: 'Planning', nodeType: 'agent', status: 'queued', lane: 1, row: 4, icon: Bot, model: 'gpt-5', outputSummary: 'Selecting code, research, and document agents' }),
    makeNode({ id: 'code', label: 'Code Agent', category: 'Specialized Agent', nodeType: 'agent', status: 'queued', lane: 0, row: 5, icon: GitBranch, model: 'gpt-5-codex' }),
    makeNode({ id: 'research', label: 'Research Agent', category: 'Specialized Agent', nodeType: 'agent', status: 'queued', lane: 1, row: 5, icon: Search, model: 'gpt-5-mini' }),
    makeNode({ id: 'rag', label: 'Document RAG', category: 'Specialized Agent', nodeType: 'agent', status: 'queued', lane: 2, row: 5, icon: Database, model: 'text-embedding-3-large' }),
    makeNode({ id: 'github_mcp', label: 'GitHub MCP', category: 'MCP Tool', nodeType: 'tool', status: 'queued', lane: 0, row: 6, icon: GitBranch, toolName: 'Read Repository' }),
    makeNode({ id: 'web_mcp', label: 'Web Search MCP', category: 'MCP Tool', nodeType: 'tool', status: 'queued', lane: 1, row: 6, icon: Globe, toolName: 'Architecture Search' }),
    makeNode({ id: 'vector_mcp', label: 'Vector MCP', category: 'MCP Tool', nodeType: 'tool', status: 'queued', lane: 2, row: 6, icon: Database, toolName: 'Semantic Retrieval' }),
    makeNode({ id: 'report', label: 'Report Generator', category: 'Output Agent', nodeType: 'agent', status: 'queued', lane: 1, row: 7, icon: FileText, model: 'gpt-5' }),
    makeNode({ id: 'evaluation', label: 'Evaluation Agent', category: 'Evaluation', nodeType: 'agent', status: 'queued', lane: 1, row: 8, icon: CheckCircle, model: 'gpt-5-mini' }),
    makeNode({ id: 'final', label: 'Final Output', category: 'Output', nodeType: 'output', status: 'queued', lane: 1, row: 9, icon: CheckCircle }),
  ]

  const edges: WorkflowEdge[] = [
    { from: 'input', to: 'supervisor' },
    { from: 'supervisor', to: 'memory' },
    { from: 'memory', to: 'planner' },
    { from: 'planner', to: 'code' },
    { from: 'planner', to: 'research' },
    { from: 'planner', to: 'rag' },
    { from: 'code', to: 'github_mcp' },
    { from: 'research', to: 'web_mcp' },
    { from: 'rag', to: 'vector_mcp' },
    { from: 'github_mcp', to: 'report' },
    { from: 'web_mcp', to: 'report' },
    { from: 'vector_mcp', to: 'report' },
    { from: 'report', to: 'evaluation' },
    { from: 'evaluation', to: 'final' },
  ]

  return [
    { type: 'event', delay: 180, message: 'Supervisor accepted task and opened execution graph.', status: 'running', nodeId: 'supervisor' },
    { type: 'node', delay: 260, node: nodes[0] },
    { type: 'edge', delay: 280, edge: edges[0] },
    { type: 'status', delay: 560, nodeId: 'supervisor', status: 'running' },
    { type: 'status', delay: 1050, nodeId: 'supervisor', status: 'completed', durationMs: 410, outputSummary: 'Task routed to memory and planner.' },
    { type: 'node', delay: 1150, node: nodes[1] },
    { type: 'edge', delay: 1160, edge: edges[1] },
    { type: 'status', delay: 1360, nodeId: 'memory', status: 'running' },
    { type: 'status', delay: 1900, nodeId: 'memory', status: 'completed', durationMs: 240, outputSummary: '4 memories retrieved; 2 preferences found.' },
    { type: 'node', delay: 2040, node: nodes[2] },
    { type: 'edge', delay: 2060, edge: edges[2] },
    { type: 'status', delay: 2260, nodeId: 'planner', status: 'running' },
    { type: 'event', delay: 2600, message: 'Planner is selecting agents and assigning MCP tools.', status: 'running', nodeId: 'planner' },
    { type: 'status', delay: 3100, nodeId: 'planner', status: 'completed', durationMs: 720, outputSummary: 'Created parallel code, research, and document RAG branches.' },
    ...nodes.slice(3, 9).flatMap((node, index) => [
      { type: 'node' as const, delay: 3250 + index * 90, node },
      { type: 'edge' as const, delay: 3290 + index * 90, edge: edges[3 + index] },
    ]),
    { type: 'status', delay: 3950, nodeId: 'code', status: 'running' },
    { type: 'status', delay: 4000, nodeId: 'research', status: 'running' },
    { type: 'status', delay: 4050, nodeId: 'rag', status: 'running' },
    { type: 'status', delay: 4300, nodeId: 'github_mcp', status: 'running' },
    { type: 'status', delay: 4350, nodeId: 'web_mcp', status: 'running' },
    { type: 'status', delay: 4400, nodeId: 'vector_mcp', status: 'running' },
    { type: 'status', delay: 4950, nodeId: 'web_mcp', status: 'retrying', retryCount: 1, outputSummary: 'Search provider timeout; retrying with backup route.' },
    { type: 'status', delay: 5450, nodeId: 'github_mcp', status: 'completed', durationMs: 980, outputSummary: 'Repository tree and dependency files collected.' },
    { type: 'status', delay: 5600, nodeId: 'vector_mcp', status: 'completed', durationMs: 870, outputSummary: 'Retrieved 8 relevant knowledge chunks.' },
    { type: 'status', delay: 6100, nodeId: 'web_mcp', status: 'completed', durationMs: 1450, outputSummary: 'Collected five architecture references after retry.' },
    { type: 'status', delay: 6350, nodeId: 'code', status: 'completed', durationMs: 1820, outputSummary: 'Identified backend, frontend, and orchestration boundaries.' },
    { type: 'status', delay: 6500, nodeId: 'research', status: 'completed', durationMs: 2100, retryCount: 1, outputSummary: 'Best-practice comparison completed.' },
    { type: 'status', delay: 6650, nodeId: 'rag', status: 'completed', durationMs: 1300, outputSummary: 'Matched uploaded docs and local architecture notes.' },
    { type: 'node', delay: 6900, node: nodes[9] },
    ...edges.slice(9, 12).map((edge, index) => ({ type: 'edge' as const, delay: 6950 + index * 70, edge })),
    { type: 'status', delay: 7250, nodeId: 'report', status: 'running' },
    { type: 'status', delay: 8050, nodeId: 'report', status: 'completed', durationMs: 780, outputSummary: 'Architecture report drafted with agent contributions.', metrics: { tokens: 6400, cost: 0.19 } },
    { type: 'node', delay: 8250, node: nodes[10] },
    { type: 'edge', delay: 8280, edge: edges[12] },
    { type: 'status', delay: 8550, nodeId: 'evaluation', status: 'running' },
    { type: 'status', delay: 9250, nodeId: 'evaluation', status: 'completed', durationMs: 620, outputSummary: 'Evaluation passed; groundedness and completeness verified.', metrics: { score: 92 } },
    { type: 'node', delay: 9450, node: nodes[11] },
    { type: 'edge', delay: 9480, edge: edges[13] },
    { type: 'status', delay: 9750, nodeId: 'final', status: 'completed', durationMs: 80, outputSummary: 'Final report ready.' },
    {
      type: 'complete',
      delay: 10100,
      evaluationScore: 92,
      reportPreview: 'Architecture report generated: React/Vite frontend, FastAPI backend, LangGraph orchestration, RAG agents, and MCP tool boundaries. Evaluation score: 92/100.',
    },
  ]
}

export function applyScheduledEvent(run: WorkflowRunState, event: ScheduledWorkflowEvent): WorkflowRunState {
  if (event.type === 'event') {
    return { ...run, events: [makeEvent(event.message, event.status, event.nodeId), ...run.events] }
  }

  if (event.type === 'node') {
    if (run.nodes.some(node => node.id === event.node.id)) return run
    return {
      ...run,
      nodes: [...run.nodes, event.node],
      events: [makeEvent(`${event.node.label} added to workflow.`, event.node.status, event.node.id), ...run.events],
    }
  }

  if (event.type === 'edge') {
    if (run.edges.some(edge => edge.from === event.edge.from && edge.to === event.edge.to)) return run
    return { ...run, edges: [...run.edges, event.edge] }
  }

  if (event.type === 'status') {
    return {
      ...run,
      nodes: run.nodes.map(node => node.id === event.nodeId
        ? {
            ...node,
            status: event.status,
            durationMs: event.durationMs ?? node.durationMs,
            outputSummary: event.outputSummary ?? node.outputSummary,
            retryCount: event.retryCount ?? node.retryCount,
            metrics: event.metrics ?? node.metrics,
          }
        : node
      ),
      events: [makeEvent(`${event.nodeId} ${event.status.replace(/_/g, ' ')}.`, event.status, event.nodeId), ...run.events],
    }
  }

  return {
    ...run,
    status: 'completed',
    reportPreview: event.reportPreview,
    evaluationScore: event.evaluationScore,
    events: [makeEvent('Workflow completed and final report is ready.', 'completed', 'final'), ...run.events],
  }
}

function makeNode(node: WorkflowNodeData): WorkflowNodeData {
  return { retryCount: 0, ...node }
}

function makeEvent(message: string, status: NodeStatus, nodeId?: string): WorkflowEvent {
  return {
    id: `evt_${Date.now()}_${Math.random().toString(16).slice(2)}`,
    time: new Date().toLocaleTimeString(),
    nodeId,
    message,
    status,
  }
}
