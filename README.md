# orchestrator_system

A runnable starter project for a central chat interface that routes user requests to specialized worker agents.

## Stack

- Backend: FastAPI
- Workflow shape: supervisor/router with LangGraph-ready agent boundaries
- Frontend: React + Vite + TypeScript
- Agents:
  - General Chat
  - Deep Research
  - Document RAG
  - YouTube RAG
  - Code & Development
  - Data Analyst

## Quick Start

### Backend

```powershell
copy .env.example .env
notepad .env
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
cd backend
uvicorn app.main:app --reload --port 8000
```

Put your Groq key in the root `.env` file:

```env
GROQ_API_KEY=gsk_your_key_here
GROQ_MODEL=llama-3.3-70b-versatile
POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/orchestrator?sslmode=disable
```

`POSTGRES_URL` enables LangGraph's PostgreSQL checkpointer. `POSTGRES_CHECKPOINTER_URI` or `DATABASE_URL` can be used instead. If no Postgres URL is set, the backend uses an in-memory checkpointer for local development.

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open the Vite URL shown in the terminal, usually `http://localhost:5173`.

## Notes

- The Data Analyst agent never mutates uploaded source files. It writes derived outputs into `backend/storage/outputs`.
- The RAG and research integrations are scaffolded with clean interfaces so API providers can be added without changing the supervisor contract.
- Manual agent override is supported from the frontend and respected by the backend router.
- The chat API invokes a compiled LangGraph workflow: router, selected agent, validation, retry, fallback, and final response.
- The high-level planned orchestrator flow is documented in [docs/orchestrator-flow.md](docs/orchestrator-flow.md).
