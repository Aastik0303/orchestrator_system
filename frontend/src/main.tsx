import React, { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Bot,
  Brain,
  FileUp,
  Menu,
  MessageSquarePlus,
  PanelLeftClose,
  Send,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import "./styles.css";

type AgentName =
  | "auto"
  | "general_chat"
  | "deep_research"
  | "document_rag"
  | "youtube_rag"
  | "code_dev"
  | "data_analyst";

type ChatTurn = {
  role: "user" | "assistant";
  content: string;
  agent?: AgentName;
};

type ChatSession = {
  id: string;
  title: string;
  turns: ChatTurn[];
  createdAt: number;
  updatedAt: number;
};

const STORAGE_KEY = "orchestrator.chat.sessions.v1";
const ACTIVE_SESSION_KEY = "orchestrator.chat.activeSession.v1";

const agents: { value: AgentName; label: string }[] = [
  { value: "auto", label: "Auto" },
  { value: "general_chat", label: "General Chat" },
  { value: "document_rag", label: "Document RAG" },
  { value: "youtube_rag", label: "YouTube RAG" },
  { value: "code_dev", label: "Code & Dev" },
  { value: "data_analyst", label: "Data Analyst" },
];

const initialTurn: ChatTurn = {
  role: "assistant",
  agent: "general_chat",
  content: "General Chat Agent ready. Send a request or attach files to start routing.",
};

function createSession(): ChatSession {
  const now = Date.now();
  return {
    id: crypto.randomUUID(),
    title: "New chat",
    turns: [initialTurn],
    createdAt: now,
    updatedAt: now,
  };
}

function loadSessions(): ChatSession[] {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return [createSession()];
    const sessions = JSON.parse(saved) as ChatSession[];
    return sessions.length ? sessions : [createSession()];
  } catch {
    return [createSession()];
  }
}

function formatSessionTime(timestamp: number) {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(timestamp);
}

function titleFromMessage(message: string) {
  const trimmed = message.trim().replace(/\s+/g, " ");
  return trimmed.length > 42 ? `${trimmed.slice(0, 42)}...` : trimmed || "New chat";
}

function App() {
  const [sessions, setSessions] = useState<ChatSession[]>(loadSessions);
  const [activeSessionId, setActiveSessionId] = useState(() => {
    return localStorage.getItem(ACTIVE_SESSION_KEY) ?? sessions[0].id;
  });
  const [message, setMessage] = useState("");
  const [deepResearch, setDeepResearch] = useState(false);
  const [agentOverride, setAgentOverride] = useState<AgentName>("auto");
  const [files, setFiles] = useState<FileList | null>(null);
  const [loading, setLoading] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);

  const activeSession = useMemo(() => {
    return sessions.find((session) => session.id === activeSessionId) ?? sessions[0];
  }, [activeSessionId, sessions]);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions));
    localStorage.setItem(ACTIVE_SESSION_KEY, activeSession.id);
  }, [activeSession.id, sessions]);

  useEffect(() => {
    messagesRef.current?.scrollTo({
      top: messagesRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [activeSession.turns.length, loading]);

  function updateActiveSession(updater: (session: ChatSession) => ChatSession) {
    setSessions((current) =>
      current.map((session) => (session.id === activeSession.id ? updater(session) : session)),
    );
  }

  function resetFileInput() {
    setFiles(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }

  function newChat() {
    const session = createSession();
    setSessions((current) => [session, ...current]);
    setActiveSessionId(session.id);
    setMessage("");
    resetFileInput();
    setSidebarOpen(false);
  }

  function clearChat() {
    updateActiveSession((session) => ({
      ...session,
      title: "New chat",
      turns: [initialTurn],
      updatedAt: Date.now(),
    }));
    setMessage("");
    resetFileInput();
  }

  function deleteSession(sessionId: string) {
    setSessions((current) => {
      if (current.length === 1) return [createSession()];
      const next = current.filter((session) => session.id !== sessionId);
      if (activeSessionId === sessionId) {
        setActiveSessionId(next[0].id);
      }
      return next;
    });
  }

  function selectSession(sessionId: string) {
    setActiveSessionId(sessionId);
    setSidebarOpen(false);
    resetFileInput();
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!message.trim() || loading) return;

    const form = new FormData();
    form.append("message", message);
    form.append("deep_research", String(deepResearch));
    form.append("agent_override", agentOverride);

    Array.from(files ?? []).forEach((file) => form.append("files", file));

    const userMessage = message;
    const shouldRename = activeSession.title === "New chat";
    updateActiveSession((session) => ({
      ...session,
      title: shouldRename ? titleFromMessage(userMessage) : session.title,
      turns: [...session.turns, { role: "user", content: userMessage }],
      updatedAt: Date.now(),
    }));
    setMessage("");
    setLoading(true);

    try {
      const response = await fetch("http://localhost:8000/api/chat", {
        method: "POST",
        body: form,
      });
      const data = await response.json();
      updateActiveSession((session) => ({
        ...session,
        turns: [
          ...session.turns,
          {
            role: "assistant",
            agent: data.active_agent,
            content: data.response,
          },
        ],
        updatedAt: Date.now(),
      }));
    } catch {
      updateActiveSession((session) => ({
        ...session,
        turns: [
          ...session.turns,
          {
            role: "assistant",
            agent: "general_chat",
            content: "Backend is unreachable. Start FastAPI on http://localhost:8000.",
          },
        ],
        updatedAt: Date.now(),
      }));
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="shell">
      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="sidebarTop">
          <div className="brand">
            <Brain size={28} />
            <div>
              <h1>Orchestrator</h1>
              <p>Supervisor routing for specialist agents</p>
            </div>
          </div>
          <button className="iconButton mobileOnly" type="button" onClick={() => setSidebarOpen(false)} title="Close menu">
            <X size={20} />
          </button>
        </div>

        <button className="primaryAction" type="button" onClick={newChat}>
          <MessageSquarePlus size={18} />
          <span>New Chat</span>
        </button>

        <div className="sessionBlock">
          <div className="sidebarLabel">Chat history</div>
          <div className="sessions">
            {sessions.map((session) => (
              <button
                className={`sessionItem ${session.id === activeSession.id ? "active" : ""}`}
                key={session.id}
                type="button"
                onClick={() => selectSession(session.id)}
              >
                <span className="sessionText">
                  <span className="sessionTitle">{session.title}</span>
                  <span className="sessionMeta">{formatSessionTime(session.updatedAt)}</span>
                </span>
                <span
                  className="sessionDelete"
                  role="button"
                  tabIndex={0}
                  title="Delete chat"
                  onClick={(event) => {
                    event.stopPropagation();
                    deleteSession(session.id);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      event.stopPropagation();
                      deleteSession(session.id);
                    }
                  }}
                >
                  <Trash2 size={15} />
                </span>
              </button>
            ))}
          </div>
        </div>

        <div className="controls">
          <label className="field">
            <span>Agent override</span>
            <select value={agentOverride} onChange={(event) => setAgentOverride(event.target.value as AgentName)}>
              {agents.map((agent) => (
                <option key={agent.value} value={agent.value}>
                  {agent.label}
                </option>
              ))}
            </select>
          </label>

          <label className="toggle">
            <input
              type="checkbox"
              checked={deepResearch}
              onChange={(event) => setDeepResearch(event.target.checked)}
            />
            <Sparkles size={18} />
            <span>Deep Research</span>
          </label>

          <label className="filePicker">
            <FileUp size={20} />
            <span>{files?.length ? `${files.length} file selected` : "Attach files"}</span>
            <input ref={fileInputRef} type="file" multiple onChange={(event) => setFiles(event.target.files)} />
          </label>

          <button className="clearButton" type="button" onClick={clearChat}>
            <Trash2 size={18} />
            <span>Clear Chat</span>
          </button>
        </div>
      </aside>

      {sidebarOpen && <button className="overlay" type="button" onClick={() => setSidebarOpen(false)} aria-label="Close menu" />}

      <section className="chat">
        <header className="chatHeader">
          <button className="iconButton mobileOnly" type="button" onClick={() => setSidebarOpen(true)} title="Open menu">
            <Menu size={21} />
          </button>
          <div className="chatTitleGroup">
            <h2>{activeSession.title}</h2>
            <p>{agentOverride === "auto" ? "Auto routing enabled" : `Manual: ${agentOverride.replace("_", " ")}`}</p>
          </div>
          <button className="iconButton desktopOnly" type="button" onClick={clearChat} title="Clear current chat">
            <PanelLeftClose size={20} />
          </button>
        </header>

        <div className="messages" ref={messagesRef}>
          {activeSession.turns.map((turn, index) => (
            <article className={`message ${turn.role}`} key={`${activeSession.id}-${turn.role}-${index}`}>
              <div className="avatar">{turn.role === "assistant" ? <Bot size={18} /> : "You"}</div>
              <div className="bubbleWrap">
                {turn.agent && <span className="agentLabel">Active: {turn.agent.replace("_", " ")}</span>}
                <p>{turn.content}</p>
              </div>
            </article>
          ))}
          {loading && (
            <article className="message assistant">
              <div className="avatar">
                <Bot size={18} />
              </div>
              <div className="bubbleWrap">
                <span className="agentLabel">Thinking</span>
                <p className="typing">Routing request and preparing a response...</p>
              </div>
            </article>
          )}
        </div>

        <form className="composer" onSubmit={submit}>
          <textarea
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="Ask for analysis, code help, document answers, YouTube insights..."
            rows={3}
          />
          <button type="submit" disabled={loading || !message.trim()} title="Send message">
            <Send size={20} />
          </button>
        </form>
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
