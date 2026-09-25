import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AppHeader } from '@/components/layout/AppHeader'
import { useTheme } from '@/hooks/useTheme'

const DashboardPage = lazy(() => import('@/components/dashboard/DashboardPage').then(module => ({ default: module.DashboardPage })))
const WorkspacePage = lazy(() => import('@/components/workspace/WorkspacePage').then(module => ({ default: module.WorkspacePage })))
const KnowledgePage = lazy(() => import('@/pages/KnowledgePage').then(module => ({ default: module.KnowledgePage })))
const AgentsPage = lazy(() => import('@/pages/agents').then(module => ({ default: module.AgentsPage })))
const ToolsPage = lazy(() => import('@/pages/ToolsPage').then(module => ({ default: module.ToolsPage })))
const RunsPage = lazy(() => import('@/pages/RunsPage').then(module => ({ default: module.RunsPage })))
const ReportsPage = lazy(() => import('@/pages/ReportsPage').then(module => ({ default: module.ReportsPage })))
const EvaluationsPage = lazy(() => import('@/pages/EvaluationsPage').then(module => ({ default: module.EvaluationsPage })))
const SettingsPage = lazy(() => import('@/pages/SettingsPage').then(module => ({ default: module.SettingsPage })))

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
        <Suspense fallback={<div className="route-loading">Loading workspace...</div>}>
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/workspace" element={<WorkspacePage />} />
            <Route path="/knowledge" element={<KnowledgePage />} />
            <Route path="/agents" element={<AgentsPage />} />
            <Route path="/tools" element={<ToolsPage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/reports" element={<ReportsPage />} />
            <Route path="/evaluations" element={<EvaluationsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
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
