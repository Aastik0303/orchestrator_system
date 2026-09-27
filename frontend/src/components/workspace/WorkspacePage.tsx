import { Bot, Brain, CircleAlert, Clock, Database, FileText, GitBranch, Maximize2, MessageSquarePlus, Plus, RefreshCw, Route, Search, Send, ShieldCheck, Table, Trash2, User, Wrench, X } from 'lucide-react'
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow, useEdgesState, useNodesState, type Edge, type Node, type NodeProps, type ReactFlowInstance } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import 'highlight.js/styles/github-dark.css'
import { Button } from '@/components/ui/Button'
import {
  deleteChatSession,
  getRun,
  getBackendHealth,
  getChatMessages,
  getChatSessions,
  startChatRun,
  subscribeToRun,
  uploadKnowledgeDocuments,
  type BackendWorkflowEvent,
  type ChatSession,
} from '@/services/apiClient'
import type { NodeStatus, WorkflowEvent, WorkflowNodeData, WorkflowRunState } from '@/types/workflow'

type ChatMessage = {
  role: 'assistant' | 'user'
  text: string
  attachmentName?: string
}

const starterMessages: ChatMessage[] = []
const MarkdownMessage = lazy(() => import('@/components/chat/MarkdownMessage').then(module => ({ default: module.MarkdownMessage })))

export function WorkspacePage() {
  const [draft, setDraft] = useState('')
  const [messages, setMessages] = useState(starterMessages)
  const [chatSessions, setChatSessions] = useState<ChatSession[]>([])
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [deletingSessionId, setDeletingSessionId] = useState<string | null>(null)
  const [chatStackError, setChatStackError] = useState<string | null>(null)
  const [pendingDocuments, setPendingDocuments] = useState<{ id: string; name: string }[]>([])
  const [workflowOpen, setWorkflowOpen] = useState(false)
  const [workflowFitRequest, setWorkflowFitRequest] = useState(0)
  const [showWorkflowJson, setShowWorkflowJson] = useState(false)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [run, setRun] = useState<WorkflowRunState>(() => ({
    id: 'no_active_run',
    status: 'idle',
    nodes: [],
    edges: [],
    events: [],
  }))
  const [backendStatus, setBackendStatus] = useState<'checking' | 'connected' | 'disconnected'>('checking')
  const fileInputRef = useRef<HTMLInputElement>(null)
  const messagesViewportRef = useRef<HTMLDivElement>(null)
  const runSocketRef = useRef<WebSocket | null>(null)
  const selectedNode = useMemo(() => run.nodes.find(node => node.id === selectedNodeId) ?? run.nodes[run.nodes.length - 1], [run.nodes, selectedNodeId])

  useEffect(() => () => runSocketRef.current?.close(), [])

  useEffect(() => {
    getBackendHealth()
      .then(() => setBackendStatus('connected'))
      .catch(() => setBackendStatus('disconnected'))
  }, [])

  useEffect(() => {
    loadChatStack()
  }, [])

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      messagesViewportRef.current?.scrollTo({
        top: messagesViewportRef.current.scrollHeight,
        behavior: 'smooth',
      })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [messages])

  async function loadChatStack(selectSessionId?: string) {
    try {
      const result = await getChatSessions()
      setBackendStatus('connected')
      setChatStackError(null)
      setChatSessions(result.sessions)
      const nextSessionId = selectSessionId || activeSessionId || result.sessions[0]?.id || null
      setActiveSessionId(nextSessionId)
      if (nextSessionId) {
        await loadSessionMessages(nextSessionId)
      } else {
        setMessages([])
      }
    } catch (error) {
      setBackendStatus('disconnected')
      setChatStackError(error instanceof Error ? error.message : 'Could not load chat stack.')
    }
  }

  async function loadSessionMessages(sessionId: string) {
    const result = await getChatMessages(sessionId)
    setMessages(result.messages.map(message => ({
      role: message.role,
      text: message.content,
      attachmentName: message.attachment_name || undefined,
    })))
  }

  async function handleNewChat() {
    setMessages([])
    setActiveSessionId(null)
    setChatStackError(null)
    setPendingDocuments([])
  }

  async function handleSelectChat(sessionId: string) {
    setActiveSessionId(sessionId)
    await loadSessionMessages(sessionId)
  }

  async function handleDeleteChat(sessionId: string) {
    const session = chatSessions.find(item => item.id === sessionId)
    if (!window.confirm(`Delete "${session?.title || 'this chat'}" and all of its messages?`)) return

    setDeletingSessionId(sessionId)
    setChatStackError(null)
    try {
      await deleteChatSession(sessionId)
      const result = await getChatSessions()
      setBackendStatus('connected')
      setChatSessions(result.sessions)

      if (activeSessionId === sessionId) {
        const nextSessionId = result.sessions[0]?.id || null
        setActiveSessionId(nextSessionId)
        if (nextSessionId) {
          await loadSessionMessages(nextSessionId)
        } else {
          setMessages([])
        }
      }
    } catch (error) {
      setChatStackError(error instanceof Error ? error.message : 'Could not delete the chat.')
    } finally {
      setDeletingSessionId(null)
    }
  }

  async function handleFileUpload(files: FileList | null) {
    if (!files?.length) return

    const uploadFiles = Array.from(files)
    const documentList = uploadFiles.map(file => file.name).join(', ')
    setMessages(currentMessages => [
      ...currentMessages,
      {
        role: 'user',
        text: uploadFiles.length === 1 ? `Uploading ${uploadFiles[0].name}` : `Uploading ${uploadFiles.length} documents`,
        attachmentName: documentList,
      },
    ])

    try {
      const result = await uploadKnowledgeDocuments(uploadFiles)
      setBackendStatus('connected')
      setPendingDocuments(result.documents.map(document => ({ id: document.id, name: document.name })))
      setMessages(currentMessages => [
        ...currentMessages,
        {
          role: 'assistant',
          text: `${result.documents.length === 1 ? 'Document is' : 'Documents are'} stored in backend Knowledge and attached to the next task.`,
        },
      ])
    } catch (error) {
      setBackendStatus('disconnected')
      const message = error instanceof Error ? error.message : 'Upload failed.'
      setMessages(currentMessages => [
        ...currentMessages,
        {
          role: 'assistant',
          text: `Document upload failed: ${message}`,
        },
      ])
    }
  }

  async function handleSubmit() {
    const task = draft.trim()
    if (!task) return

    runSocketRef.current?.close()
    const attachedDocuments = pendingDocuments
    const attachmentName = attachedDocuments.map(document => document.name).join(', ')
    setDraft('')
    setWorkflowOpen(true)
    setSelectedNodeId(null)
    setMessages(currentMessages => [
      ...currentMessages,
      { role: 'user', text: task, attachmentName: attachmentName || undefined },
      { role: 'assistant', text: 'Workflow started. Live agent events will appear in the canvas.' },
    ])

    try {
      setBackendStatus('connected')
      const started = await startChatRun({
        message: task,
        files: [],
        documentIds: attachedDocuments.map(document => document.id),
        sessionId: activeSessionId || undefined,
      })
      setPendingDocuments([])
      setActiveSessionId(started.session_id)
      setRun({ id: started.run_id, status: 'queued', nodes: [], edges: [], events: [] })
      let finalized = false
      const finalizeRun = async () => {
        if (finalized) return
        finalized = true
        try {
          const completedRun = await getRun(started.run_id)
          setRun(currentRun => ({
            ...currentRun,
            status: normalizeNodeStatus(completedRun.status),
            reportPreview: completedRun.response,
            evaluationScore: completedRun.evaluation?.overall_score,
          }))
          await loadChatStack(started.session_id)
        } catch (error) {
          setChatStackError(error instanceof Error ? error.message : 'Could not load the completed run.')
        }
      }
      runSocketRef.current = subscribeToRun(
        started.run_id,
        event => {
          if (event.type === 'stream_closed') return
          setRun(currentRun => applyBackendWorkflowEvent(currentRun, event))
          if (event.node_id) setSelectedNodeId(event.node_id)
        },
        finalizeRun,
        () => setBackendStatus('disconnected'),
      )
    } catch (error) {
      setBackendStatus('disconnected')
      const message = error instanceof Error ? error.message : 'Backend request failed.'
      setRun(currentRun => ({ ...currentRun, status: 'failed' }))
      setMessages(currentMessages => [
        ...currentMessages,
        { role: 'assistant', text: `Backend error: ${message}` },
      ])
    }
  }

  return (
    <div className="workspace-page">
      <div className="workspace-split">
        <aside className="workspace-left-rail">
          <section className="chat-stack-panel" aria-label="Chat stack">
            <div className="chat-stack-header">
              <div>
                <h3>Chat Stack</h3>
                <p>{chatSessions.length} saved chats</p>
              </div>
              <button className="new-chat-button" onClick={handleNewChat} title="New chat">
                <MessageSquarePlus className="h-4 w-4" />
              </button>
            </div>
            {chatStackError && <p className="chat-stack-error">{chatStackError}</p>}
            <div className="chat-stack-list">
              {chatSessions.length === 0 ? (
                <div className="chat-stack-empty">No saved chats yet.</div>
              ) : (
                chatSessions.map(session => (
                  <div
                    key={session.id}
                    className={`chat-stack-item ${activeSessionId === session.id ? 'chat-stack-item-active' : ''}`}
                  >
                    <button className="chat-stack-select" type="button" onClick={() => handleSelectChat(session.id)}>
                      <strong>{session.title}</strong>
                      <span>{session.message_count} messages</span>
                    </button>
                    <button
                      className="chat-stack-delete"
                      type="button"
                      title={`Delete ${session.title}`}
                      aria-label={`Delete ${session.title}`}
                      disabled={deletingSessionId === session.id}
                      onClick={() => handleDeleteChat(session.id)}
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                ))
              )
              }
            </div>
          </section>

          <button className="workflow-mini-card" onClick={() => setWorkflowOpen(true)}>
            <div className="workflow-mini-header">
              <span className="node-icon"><Bot className="h-4 w-4" /></span>
              <span>
                <strong>Live Agent Workflow</strong>
                <small>{backendStatus === 'connected' ? `${run.status} - backend online` : backendStatus === 'checking' ? 'checking backend' : 'backend offline'}</small>
              </span>
              <Maximize2 className="h-4 w-4" />
            </div>
            <div className="workflow-mini-rail">
              {run.nodes.length === 0 && <span className="workflow-mini-empty">No active backend run</span>}
              {run.nodes.slice(0, 5).map(node => {
                const Icon = node.icon
                return (
                  <span key={node.id} className={`workflow-mini-node workflow-mini-node-${node.status}`}>
                    <Icon className="h-3 w-3" />
                    <span>{node.label}</span>
                  </span>
                )
              })}
            </div>
          </button>
        </aside>

        <section className="conversation-panel" aria-label="User conversation">
          <div className="panel-toolbar">
            <div>
              <h3>Conversation</h3>
              <p>Task intake and final responses</p>
            </div>
            <span className="connection-pill">Live</span>
          </div>
          <div className="workspace-messages" ref={messagesViewportRef}>
            {messages.length === 0 ? (
              <div className="conversation-empty">
                <Bot className="h-5 w-5" />
                <div>
                  <strong>No conversation yet</strong>
                  <span>Submit a task or upload a document to create real backend data.</span>
                </div>
              </div>
            ) : (
              messages.map((message, index) => (
                <div key={`${message.role}-${index}`} className={`workspace-message workspace-message-${message.role}`}>
                  <div className="workspace-avatar">
                    {message.role === 'assistant' ? <Bot className="h-4 w-4" /> : <User className="h-4 w-4" />}
                  </div>
                  <div className="workspace-message-content">
                    <Suspense fallback={<span>{message.text}</span>}>
                      <MarkdownMessage content={message.text} />
                    </Suspense>
                    {message.attachmentName && (
                      <span className="message-attachment">
                        <FileText className="h-4 w-4" />
                        {message.attachmentName}
                      </span>
                    )}
                  </div>
                </div>
              ))
            )}
          </div>
        </section>
      </div>

      {workflowOpen && (
        <div className="workflow-modal-backdrop" role="dialog" aria-modal="true" aria-label="Live agent workflow">
          <section className="workflow-modal">
            <div className="panel-toolbar">
              <div>
                <h3>Live Agent Workflow</h3>
                <p>{run.id} - {run.status}</p>
              </div>
              <div className="canvas-actions">
                <button type="button" onClick={() => setWorkflowFitRequest(request => request + 1)}>Fit</button>
                <button
                  type="button"
                  className={showWorkflowJson ? 'canvas-action-active' : ''}
                  onClick={() => setShowWorkflowJson(isVisible => !isVisible)}
                >
                  JSON
                </button>
                <button className="modal-close-button" onClick={() => setWorkflowOpen(false)} aria-label="Close workflow popup">
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>
            <div className="workflow-modal-body">
              <WorkflowCanvas
                run={run}
                selectedNodeId={selectedNode?.id}
                fitRequest={workflowFitRequest}
                onSelectNode={nodeId => {
                  setSelectedNodeId(nodeId)
                  setShowWorkflowJson(false)
                }}
              />
              <aside className="workflow-inspector">
                {showWorkflowJson ? <WorkflowJsonView run={run} /> : selectedNode && <NodeInspector node={selectedNode} />}
                <div className="workflow-timeline">
                  <h4>Live Events</h4>
                  {run.events.map(event => (
                    <button key={event.id} className={`workflow-event workflow-event-${event.status}`} onClick={() => {
                      if (event.nodeId) {
                        setSelectedNodeId(event.nodeId)
                        setShowWorkflowJson(false)
                      }
                    }}>
                      <span>{event.time}</span>
                      <strong>{event.message}</strong>
                    </button>
                  ))}
                </div>
              </aside>
            </div>
          </section>
        </div>
      )}

      <form className="workspace-composer" onSubmit={event => {
        event.preventDefault()
        handleSubmit()
      }}>
        <span className="workspace-composer-spacer" aria-hidden="true" />
        <input
          ref={fileInputRef}
          className="doc-upload-input"
          type="file"
          multiple
          accept=".pdf,.docx,.txt,.md,.csv,.xlsx,.xls"
          onChange={event => {
            handleFileUpload(event.target.files)
            event.target.value = ''
          }}
        />
        <button
          className="upload-doc-button"
          type="button"
          onClick={() => fileInputRef.current?.click()}
          title="Upload document to Knowledge"
          aria-label="Upload document to Knowledge"
        >
          <Plus className="h-5 w-5" />
        </button>
        <textarea value={draft} onChange={event => setDraft(event.target.value)} placeholder="Describe the task for your agents..." />
        <Button type="submit" title="Send">
          <Send className="h-4 w-4" />
        </Button>
      </form>

    </div>
  )
}

function normalizeNodeStatus(status?: string | null): NodeStatus {
  const validStatuses: NodeStatus[] = ['idle', 'queued', 'running', 'completed', 'failed', 'retrying', 'skipped', 'waiting_for_approval', 'cancelled']
  // Backend step states that have no direct canvas equivalent.
  if (status === 'blocked') return 'skipped'
  if (status === 'timeout') return 'failed'
  return validStatuses.includes(status as NodeStatus) ? status as NodeStatus : 'running'
}

function workflowIcon(event: BackendWorkflowEvent) {
  const agent = String(event.details?.agent || '')
  if (event.node_type === 'input') return User
  if (event.node_type === 'output') return FileText
  if (event.node_type === 'tool') return Wrench
  if (agent === 'memory' || event.node_id?.includes('memory')) return Brain
  if (agent === 'planner' || event.node_id === 'planner') return Route
  if (agent === 'evaluation' || event.node_id?.includes('evaluation')) return ShieldCheck
  if (agent === 'report_generator' || event.node_id?.includes('report')) return FileText
  if (agent === 'code_dev') return GitBranch
  if (agent === 'data_analyst' || agent === 'sql_agent') return Table
  if (agent === 'document_rag') return Database
  if (agent === 'deep_research' || agent === 'youtube_rag') return Search
  if (event.node_type === 'logic') return Route
  return Bot
}

function applyBackendWorkflowEvent(run: WorkflowRunState, event: BackendWorkflowEvent): WorkflowRunState {
  const details = event.details || {}
  const nodeId = event.node_id || undefined
  const status = normalizeNodeStatus(event.status)
  const dependencies = Array.isArray(details.depends_on) ? details.depends_on.filter((item): item is string => typeof item === 'string') : []
  let nodes = run.nodes
  let edges = run.edges

  if (nodeId) {
    const currentNode = nodes.find(node => node.id === nodeId)
    const dependencyRows = dependencies.map(id => nodes.find(node => node.id === id)?.row ?? -1)
    const row = currentNode?.row ?? Math.max(0, ...dependencyRows) + 1
    const lanePeers = nodes.filter(node => node.row === row && node.id !== nodeId)
    const nodeUpdate: WorkflowNodeData = {
      id: nodeId,
      label: event.label || currentNode?.label || nodeId.replace(/_/g, ' '),
      category: event.node_type ? `${event.node_type[0].toUpperCase()}${event.node_type.slice(1)} node` : currentNode?.category || 'Workflow node',
      nodeType: ['input', 'agent', 'tool', 'logic', 'output'].includes(event.node_type || '') ? event.node_type as WorkflowNodeData['nodeType'] : currentNode?.nodeType || 'logic',
      status,
      lane: currentNode?.lane ?? Math.min(3, lanePeers.length),
      row,
      icon: currentNode?.icon || workflowIcon(event),
      model: typeof details.model === 'string' ? details.model : currentNode?.model,
      toolName: typeof details.tool === 'string' ? details.tool : currentNode?.toolName,
      durationMs: typeof details.duration_ms === 'number' ? details.duration_ms : currentNode?.durationMs,
      retryCount: typeof details.retry_count === 'number' ? details.retry_count : currentNode?.retryCount || 0,
      inputSummary: typeof details.input === 'string' ? details.input : currentNode?.inputSummary,
      outputSummary: typeof details.output === 'string' ? details.output : currentNode?.outputSummary,
      error: typeof details.error === 'string' ? details.error : currentNode?.error,
      metrics: {
        ...currentNode?.metrics,
        score: typeof details.evaluation_score === 'number' ? details.evaluation_score : currentNode?.metrics?.score,
      },
    }
    nodes = currentNode ? nodes.map(node => node.id === nodeId ? nodeUpdate : node) : [...nodes, nodeUpdate]
    const edgeKeys = new Set(edges.map(edge => `${edge.from}:${edge.to}`))
    const nextEdges = dependencies
      .filter(dependency => dependency !== nodeId && !edgeKeys.has(`${dependency}:${nodeId}`))
      .map(dependency => ({ from: dependency, to: nodeId }))
    edges = [...edges, ...nextEdges]
  }

  const workflowEvent: WorkflowEvent = {
    id: event.id || `evt_${event.sequence || Date.now()}`,
    time: event.timestamp ? new Date(event.timestamp).toLocaleTimeString() : new Date().toLocaleTimeString(),
    nodeId,
    message: `${event.label || nodeId || 'Workflow'}: ${event.type.replace(/_/g, ' ')}`,
    status,
  }
  const terminalStatus = event.type === 'workflow_completed' ? 'completed' : event.type === 'workflow_failed' || event.type === 'workflow_timeout' || event.type === 'workflow_blocked' ? 'failed' : event.type === 'workflow_cancelled' ? 'cancelled' : run.status === 'queued' ? 'running' : run.status
  return {
    ...run,
    status: terminalStatus,
    nodes,
    edges,
    events: [workflowEvent, ...run.events],
    reportPreview: event.type === 'workflow_completed' && typeof details.output === 'string' ? details.output : run.reportPreview,
    evaluationScore: typeof details.evaluation_score === 'number' ? details.evaluation_score : run.evaluationScore,
  }
}

type WorkflowNodePosition = {
  x: number
  y: number
}

function getDefaultNodePosition(node: WorkflowNodeData): WorkflowNodePosition {
  return {
    x: 88 + node.lane * 250,
    y: 28 + node.row * 92,
  }
}

function WorkflowCanvas({
  run,
  selectedNodeId,
  fitRequest,
  onSelectNode,
}: {
  run: WorkflowRunState
  selectedNodeId?: string
  fitRequest: number
  onSelectNode: (nodeId: string) => void
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<WorkflowFlowNodeData>>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const flowRef = useRef<ReactFlowInstance<Node<WorkflowFlowNodeData>, Edge> | null>(null)

  useEffect(() => {
    setNodes(currentNodes => {
      const currentById = new Map(currentNodes.map(node => [node.id, node]))
      return run.nodes.map(workflowNode => {
        const current = currentById.get(workflowNode.id)
        return {
          id: workflowNode.id,
          type: 'workflowNode',
          position: current?.position || getDefaultNodePosition(workflowNode),
          data: { workflowNode },
          sourcePosition: Position.Bottom,
          targetPosition: Position.Top,
          selected: workflowNode.id === selectedNodeId,
        }
      })
    })
    setEdges(run.edges.map(edge => ({
      id: `${edge.from}-${edge.to}`,
      source: edge.from,
      target: edge.to,
      type: 'smoothstep',
      animated: run.nodes.find(node => node.id === edge.to)?.status === 'running',
      markerEnd: { type: MarkerType.ArrowClosed, color: '#2563eb', width: 18, height: 18 },
      style: { stroke: 'var(--app-primary)', strokeWidth: 2.4 },
      interactionWidth: 20,
      className: 'workflow-flow-edge',
    })))
  }, [run.id, run.nodes, run.edges, selectedNodeId, setEdges, setNodes])

  useEffect(() => {
    flowRef.current?.fitView({ padding: 0.22, duration: 350 })
  }, [fitRequest])

  return (
    <div className="workflow-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={workflowNodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(_event, node) => onSelectNode(node.id)}
        onInit={instance => { flowRef.current = instance }}
        fitView
        fitViewOptions={{ padding: 0.22 }}
        minZoom={0.25}
        maxZoom={1.8}
        nodesDraggable
        panOnDrag
        zoomOnScroll
      >
        <Background gap={28} size={1} />
        <MiniMap pannable zoomable className="workflow-minimap" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}

type WorkflowFlowNodeData = { workflowNode: WorkflowNodeData }

const workflowNodeTypes = { workflowNode: WorkflowFlowNode }

function WorkflowFlowNode({ data, selected }: NodeProps<Node<WorkflowFlowNodeData>>) {
  const node = data.workflowNode
  const Icon = node.icon
  return (
    <div className={`workflow-flow-node workflow-flow-node-${node.status} ${selected ? 'workflow-flow-node-selected' : ''}`}>
      <Handle type="target" position={Position.Top} className="workflow-flow-handle" isConnectable={false} />
      <span className="node-icon"><Icon className="h-4 w-4" /></span>
      <span className="node-copy">
        <strong>{node.label}</strong>
        <small>{node.outputSummary || node.inputSummary || node.category}</small>
      </span>
      <span className="node-status">
        {node.status === 'running' ? <Clock className="h-3 w-3" /> : node.status === 'retrying' ? <RefreshCw className="h-3 w-3" /> : node.status === 'failed' ? <CircleAlert className="h-3 w-3" /> : null}
        {node.status}
      </span>
      <Handle type="source" position={Position.Bottom} className="workflow-flow-handle" isConnectable={false} />
    </div>
  )
}

function WorkflowJsonView({ run }: { run: WorkflowRunState }) {
  const serializableRun = {
    id: run.id,
    status: run.status,
    reportPreview: run.reportPreview,
    evaluationScore: run.evaluationScore,
    nodes: run.nodes.map(node => ({
      id: node.id,
      label: node.label,
      category: node.category,
      nodeType: node.nodeType,
      status: node.status,
      lane: node.lane,
      row: node.row,
      model: node.model,
      toolName: node.toolName,
      inputSummary: node.inputSummary,
      outputSummary: node.outputSummary,
      durationMs: node.durationMs,
      retryCount: node.retryCount,
      error: node.error,
      metrics: node.metrics,
    })),
    edges: run.edges,
    events: run.events,
  }

  return (
    <div className="workflow-json-card">
      <div>
        <h4>Workflow JSON</h4>
        <p>Current backend run state</p>
      </div>
      <pre>{JSON.stringify(serializableRun, null, 2)}</pre>
    </div>
  )
}

function NodeInspector({ node }: { node: WorkflowNodeData }) {
  return (
    <div className="node-inspector-card">
      <div className="node-inspector-title">
        <span className={`node-inspector-dot workflow-mini-node-${node.status}`} />
        <div>
          <h4>{node.label}</h4>
          <p>{node.category}</p>
        </div>
      </div>
      <dl>
        <div><dt>Status</dt><dd>{node.status.replace(/_/g, ' ')}</dd></div>
        <div><dt>Model</dt><dd>{node.model || '-'}</dd></div>
        <div><dt>Tool</dt><dd>{node.toolName || '-'}</dd></div>
        <div><dt>Duration</dt><dd>{node.durationMs ? `${node.durationMs} ms` : '-'}</dd></div>
        <div><dt>Retries</dt><dd>{node.retryCount ?? 0}</dd></div>
        <div><dt>Tokens</dt><dd>{node.metrics?.tokens ?? '-'}</dd></div>
        <div><dt>Cost</dt><dd>{node.metrics?.cost ? `$${node.metrics.cost.toFixed(2)}` : '-'}</dd></div>
        <div><dt>Score</dt><dd>{node.metrics?.score ?? '-'}</dd></div>
      </dl>
      <p className="node-inspector-summary">{node.outputSummary || node.inputSummary || 'Waiting for execution data.'}</p>
    </div>
  )
}
