import { Bot, CheckCircle, CircleAlert, Clock, Database, FileText, GitBranch, Maximize2, MessageSquarePlus, Plus, RefreshCw, Route, Search, Send, Table, User, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent } from 'react'
import { Button } from '@/components/ui/Button'
import { createInitialWorkflowRun } from '@/services/mockWorkflowEvents'
import {
  getBackendHealth,
  getChatMessages,
  getChatSessions,
  sendChatMessage,
  uploadKnowledgeDocuments,
  type BackendAgentName,
  type ChatSession,
} from '@/services/apiClient'
import type { NodeStatus, WorkflowEdge, WorkflowEvent, WorkflowNodeData, WorkflowRunState } from '@/types/workflow'

type ChatMessage = {
  role: 'assistant' | 'user'
  text: string
  attachmentName?: string
}

const starterMessages: ChatMessage[] = []

export function WorkspacePage() {
  const [draft, setDraft] = useState('')
  const [messages, setMessages] = useState(starterMessages)
  const [chatSessions, setChatSessions] = useState<ChatSession[]>([])
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [chatStackError, setChatStackError] = useState<string | null>(null)
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
  const runTimersRef = useRef<number[]>([])
  const selectedNode = useMemo(() => run.nodes.find(node => node.id === selectedNodeId) ?? run.nodes[run.nodes.length - 1], [run.nodes, selectedNodeId])

  useEffect(() => () => {
    runTimersRef.current.forEach(timer => window.clearTimeout(timer))
  }, [])

  useEffect(() => {
    getBackendHealth()
      .then(() => setBackendStatus('connected'))
      .catch(() => setBackendStatus('disconnected'))
  }, [])

  useEffect(() => {
    loadChatStack()
  }, [])

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
  }

  async function handleSelectChat(sessionId: string) {
    setActiveSessionId(sessionId)
    await loadSessionMessages(sessionId)
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
      setMessages(currentMessages => [
        ...currentMessages,
        {
          role: 'assistant',
          text: `${result.documents.length === 1 ? 'Document is' : 'Documents are'} stored in backend Knowledge and available in the Knowledge page.`,
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

    runTimersRef.current.forEach(timer => window.clearTimeout(timer))
    runTimersRef.current = []

    const nextRun = createInitialWorkflowRun(task)
    setDraft('')
    setWorkflowOpen(true)
    setSelectedNodeId('input')
    setRun(nextRun)
    setMessages(currentMessages => [
      ...currentMessages,
      { role: 'user', text: task },
      { role: 'assistant', text: 'Backend run started. Routing request through the orchestrator and waiting for the selected agent response.' },
    ])

    playBackendLifecycle(task, 0)

    try {
      setBackendStatus('connected')
      const response = await sendChatMessage({ message: task, files: [], sessionId: activeSessionId || undefined })
      const sessionId = response.session_id || activeSessionId
      if (sessionId) setActiveSessionId(sessionId)
      completeBackendRun(response.active_agent, response.response)
      setMessages(currentMessages => [
        ...currentMessages,
        { role: 'assistant', text: response.response },
      ])
      await loadChatStack(sessionId || undefined)
    } catch (error) {
      setBackendStatus('disconnected')
      const message = error instanceof Error ? error.message : 'Backend request failed.'
      failBackendRun(message)
      setMessages(currentMessages => [
        ...currentMessages,
        { role: 'assistant', text: `Backend error: ${message}` },
      ])
    }
  }

  function scheduleRunUpdate(delay: number, update: (currentRun: WorkflowRunState) => WorkflowRunState) {
    const timer = window.setTimeout(() => setRun(update), delay)
    runTimersRef.current.push(timer)
  }

  function playBackendLifecycle(task: string, fileCount: number) {
    const routerNode = makeWorkflowNode({
      id: 'router',
      label: 'Supervisor Router',
      category: 'Backend LangGraph',
      nodeType: 'logic',
      status: 'running',
      lane: 1,
      row: 2,
      icon: Route,
      inputSummary: task,
      outputSummary: fileCount > 0 ? `${fileCount} file(s) included for routing.` : 'Choosing best agent.',
    })
    const validationNode = makeWorkflowNode({
      id: 'validation',
      label: 'Validation',
      category: 'Backend Guardrail',
      nodeType: 'logic',
      status: 'queued',
      lane: 1,
      row: 4,
      icon: CheckCircle,
      outputSummary: 'Waiting for agent output.',
    })

    scheduleRunUpdate(120, runState => addNodesAndEdges(runState, [routerNode], [{ from: 'input', to: 'router' }], 'Backend router started.', 'running', 'router'))
    scheduleRunUpdate(650, runState => updateNode(runState, 'router', { status: 'completed', durationMs: 520, outputSummary: 'Agent route selected by backend.' }))
    scheduleRunUpdate(760, runState => addNodesAndEdges(runState, [validationNode], [], 'Validation node queued.', 'queued', 'validation'))
  }

  function completeBackendRun(activeAgent: BackendAgentName, responseText: string) {
    const agentNode = backendAgentNode(activeAgent)
    const finalNode = makeWorkflowNode({
      id: 'final',
      label: 'Final Response',
      category: 'Backend Output',
      nodeType: 'output',
      status: 'completed',
      lane: 1,
      row: 5,
      icon: FileText,
      durationMs: 80,
      outputSummary: responseText,
    })

    setRun(runState => {
      let nextRun = addNodesAndEdges(
        runState,
        [{ ...agentNode, status: 'completed', outputSummary: `Backend returned ${activeAgent}.` }, finalNode],
        [
          { from: 'router', to: agentNode.id },
          { from: agentNode.id, to: 'validation' },
          { from: 'validation', to: 'final' },
        ],
        `Backend selected ${agentNode.label}.`,
        'completed',
        agentNode.id
      )
      nextRun = updateNode(nextRun, 'validation', { status: 'completed', durationMs: 120, outputSummary: 'Response passed validation.' })
      return {
        ...nextRun,
        status: 'completed',
        reportPreview: responseText,
        events: [makeWorkflowEvent('Real backend response received and rendered.', 'completed', 'final'), ...nextRun.events],
      }
    })
  }

  function failBackendRun(error: string) {
    setRun(runState => ({
      ...updateNode(runState, 'router', { status: 'failed', error, outputSummary: error }),
      status: 'failed',
      events: [makeWorkflowEvent('Backend connection failed.', 'failed', 'router'), ...runState.events],
    }))
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
                  <button
                    key={session.id}
                    className={`chat-stack-item ${activeSessionId === session.id ? 'chat-stack-item-active' : ''}`}
                    onClick={() => handleSelectChat(session.id)}
                  >
                    <strong>{session.title}</strong>
                    <span>{session.message_count} messages</span>
                  </button>
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
          <div className="workspace-messages">
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
                  <p>
                    {message.text}
                    {message.attachmentName && (
                      <span className="message-attachment">
                        <FileText className="h-4 w-4" />
                        {message.attachmentName}
                      </span>
                    )}
                  </p>
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
          accept=".pdf,.txt,.md,.doc,.docx,.csv,.json,.html"
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

function makeWorkflowNode(node: WorkflowNodeData): WorkflowNodeData {
  return { retryCount: 0, ...node }
}

function backendAgentNode(activeAgent: BackendAgentName): WorkflowNodeData {
  const config: Record<BackendAgentName, Pick<WorkflowNodeData, 'label' | 'icon' | 'category' | 'lane'>> = {
    auto: { label: 'Auto Router', icon: Route, category: 'Backend Router', lane: 1 },
    general_chat: { label: 'General Chat Agent', icon: Bot, category: 'Backend Agent', lane: 1 },
    deep_research: { label: 'Deep Research Agent', icon: Search, category: 'Backend Agent', lane: 1 },
    document_rag: { label: 'Document RAG Agent', icon: FileText, category: 'Backend Agent', lane: 1 },
    youtube_rag: { label: 'YouTube RAG Agent', icon: Search, category: 'Backend Agent', lane: 1 },
    code_dev: { label: 'Code Development Agent', icon: GitBranch, category: 'Backend Agent', lane: 1 },
    data_analyst: { label: 'Data Analyst Agent', icon: Table, category: 'Backend Agent', lane: 1 },
  }
  const agent = config[activeAgent]
  return makeWorkflowNode({
    id: `agent_${activeAgent}`,
    label: agent.label,
    category: agent.category,
    nodeType: 'agent',
    status: 'running',
    lane: agent.lane,
    row: 3,
    icon: agent.icon,
    model: activeAgent === 'auto' ? undefined : 'backend selected',
  })
}

function addNodesAndEdges(
  run: WorkflowRunState,
  nodes: WorkflowNodeData[],
  edges: WorkflowEdge[],
  eventMessage: string,
  eventStatus: NodeStatus,
  eventNodeId?: string
): WorkflowRunState {
  const existingNodeIds = new Set(run.nodes.map(node => node.id))
  const existingEdges = new Set(run.edges.map(edge => `${edge.from}-${edge.to}`))
  return {
    ...run,
    nodes: [...run.nodes, ...nodes.filter(node => !existingNodeIds.has(node.id))],
    edges: [...run.edges, ...edges.filter(edge => !existingEdges.has(`${edge.from}-${edge.to}`))],
    events: [makeWorkflowEvent(eventMessage, eventStatus, eventNodeId), ...run.events],
  }
}

function updateNode(run: WorkflowRunState, nodeId: string, update: Partial<WorkflowNodeData>): WorkflowRunState {
  return {
    ...run,
    nodes: run.nodes.map(node => node.id === nodeId ? { ...node, ...update } : node),
    events: [makeWorkflowEvent(`${nodeId} ${(update.status || 'updated').replace(/_/g, ' ')}.`, update.status || 'running', nodeId), ...run.events],
  }
}

function makeWorkflowEvent(message: string, status: NodeStatus, nodeId?: string): WorkflowEvent {
  return {
    id: `evt_${Date.now()}_${Math.random().toString(16).slice(2)}`,
    time: new Date().toLocaleTimeString(),
    nodeId,
    message,
    status,
  }
}

const workflowNodeWidth = 220
const workflowNodeHeight = 72

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

function getDefaultNodePositions(nodes: WorkflowNodeData[]) {
  return nodes.reduce<Record<string, WorkflowNodePosition>>((positions, node) => {
    positions[node.id] = getDefaultNodePosition(node)
    return positions
  }, {})
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
  const [nodePositions, setNodePositions] = useState<Record<string, WorkflowNodePosition>>({})
  const [draggingNodeId, setDraggingNodeId] = useState<string | null>(null)
  const canvasRef = useRef<HTMLDivElement>(null)
  const lastFitRequestRef = useRef(fitRequest)
  const dragRef = useRef<{
    nodeId: string
    startX: number
    startY: number
    origin: WorkflowNodePosition
  } | null>(null)

  useEffect(() => {
    setNodePositions(currentPositions => {
      const nextPositions: Record<string, WorkflowNodePosition> = {}
      run.nodes.forEach(node => {
        nextPositions[node.id] = currentPositions[node.id] || getDefaultNodePosition(node)
      })
      return nextPositions
    })
  }, [run.id, run.nodes])

  useEffect(() => {
    if (fitRequest === lastFitRequestRef.current) return

    lastFitRequestRef.current = fitRequest
    dragRef.current = null
    setDraggingNodeId(null)
    setNodePositions(getDefaultNodePositions(run.nodes))
    window.requestAnimationFrame(() => {
      canvasRef.current?.scrollTo({ left: 0, top: 0, behavior: 'smooth' })
    })
  }, [fitRequest, run.nodes])

  function getNodePosition(node: WorkflowNodeData) {
    return nodePositions[node.id] || getDefaultNodePosition(node)
  }

  function handlePointerDown(event: PointerEvent<HTMLButtonElement>, node: WorkflowNodeData) {
    if (event.button !== 0) return

    const position = getNodePosition(node)
    dragRef.current = {
      nodeId: node.id,
      startX: event.clientX,
      startY: event.clientY,
      origin: position,
    }
    setDraggingNodeId(node.id)
    onSelectNode(node.id)
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  function handlePointerMove(event: PointerEvent<HTMLButtonElement>) {
    const drag = dragRef.current
    if (!drag) return

    const nextX = Math.max(16, drag.origin.x + event.clientX - drag.startX)
    const nextY = Math.max(16, drag.origin.y + event.clientY - drag.startY)
    setNodePositions(currentPositions => ({
      ...currentPositions,
      [drag.nodeId]: { x: nextX, y: nextY },
    }))
  }

  function handlePointerEnd(event: PointerEvent<HTMLButtonElement>) {
    if (!dragRef.current) return

    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    dragRef.current = null
    setDraggingNodeId(null)
  }

  return (
    <div className="workflow-canvas" ref={canvasRef}>
      <svg className="workflow-lines" viewBox="0 0 1120 820" aria-hidden="true">
        {run.edges.map(edge => {
          const fromNode = run.nodes.find(node => node.id === edge.from)
          const toNode = run.nodes.find(node => node.id === edge.to)
          if (!fromNode || !toNode) return null
          const fromPosition = getNodePosition(fromNode)
          const toPosition = getNodePosition(toNode)
          const fromX = fromPosition.x + workflowNodeWidth / 2
          const fromY = fromPosition.y + workflowNodeHeight
          const toX = toPosition.x + workflowNodeWidth / 2
          const toY = toPosition.y
          const midY = (fromY + toY) / 2
          return <path key={`${edge.from}-${edge.to}`} d={`M${fromX} ${fromY} C${fromX} ${midY} ${toX} ${midY} ${toX} ${toY}`} />
        })}
      </svg>
      {run.nodes.map(node => {
        const Icon = node.icon
        const position = getNodePosition(node)
        return (
          <button
            key={node.id}
            className={`workflow-node workflow-node-${node.status} ${selectedNodeId === node.id ? 'workflow-node-selected' : ''} ${draggingNodeId === node.id ? 'workflow-node-dragging' : ''}`}
            style={{ '--node-x': `${position.x}px`, '--node-y': `${position.y}px` } as CSSProperties}
            onPointerDown={event => handlePointerDown(event, node)}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerEnd}
            onPointerCancel={handlePointerEnd}
          >
            <span className="node-icon"><Icon className="h-4 w-4" /></span>
            <span className="node-copy">
              <strong>{node.label}</strong>
              <small>{node.outputSummary || node.inputSummary || node.category}</small>
            </span>
            <span className="node-status">
              {node.status === 'running' ? <Clock className="h-3 w-3" /> : node.status === 'retrying' ? <RefreshCw className="h-3 w-3" /> : node.status === 'failed' ? <CircleAlert className="h-3 w-3" /> : null}
              {node.status}
            </span>
          </button>
        )
      })}
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
