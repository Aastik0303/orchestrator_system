import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AppHeader } from '@/components/layout/AppHeader'
import { DashboardPage } from '@/components/dashboard/DashboardPage'
import { WorkspacePage } from '@/components/workspace/WorkspacePage'
import { KnowledgePage } from '@/pages/KnowledgePage'
import { AgentsPage } from '@/pages/agents'
import { RunsPage } from '@/pages/RunsPage'
import { ReportsPage } from '@/pages/ReportsPage'
import { EvaluationsPage } from '@/pages/EvaluationsPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { useTheme } from '@/hooks/useTheme'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5 * 60 * 1000,
      retry: 1,
    },
  },
})

function AppLayout() {
  useTheme()

  return (
    <div className="app-shell">
      <AppHeader />
      <main className="app-main-content">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/workspace" element={<WorkspacePage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/tools" element={<Navigate to="/agents" replace />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/evaluations" element={<EvaluationsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppLayout />
      </BrowserRouter>
    </QueryClientProvider>
  )
}
