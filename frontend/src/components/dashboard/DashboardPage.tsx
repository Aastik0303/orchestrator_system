import { ArrowRight, BarChart3, CheckCircle, FileText, History, Server } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { EmptyState } from '@/components/ui/EmptyState'
import { useRuntimeSnapshot } from '@/hooks/useRuntimeSnapshot'

export function DashboardPage() {
  const navigate = useNavigate()
  const { snapshot, loading, error } = useRuntimeSnapshot()
  const runs = snapshot?.runs ?? []
  const documents = snapshot?.documents ?? []
  const reports = snapshot?.reports ?? []
  const evaluation = snapshot?.evaluation

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-2xl font-bold text-surface-900 dark:text-white">Dashboard</h2>
        <p className="mt-1 text-sm text-surface-500 dark:text-surface-400">Live backend runtime state from FastAPI.</p>
      </div>

      {error && <EmptyState title="Backend runtime unavailable" description={error} icon={<Server className="h-5 w-5" />} />}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard icon={Server} label="Backend" value={snapshot?.health === 'ok' ? 'Online' : loading ? 'Checking' : 'Offline'} />
        <MetricCard icon={History} label="Runs" value={runs.length} />
        <MetricCard icon={FileText} label="Knowledge Docs" value={documents.length} />
        <MetricCard icon={CheckCircle} label="Success Rate" value={`${evaluation?.success_rate ?? 0}%`} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Recent Backend Runs</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {runs.length === 0 ? (
              <EmptyState title="No backend runs yet" description="Send a task from Workspace to create real run data." icon={<History className="h-5 w-5" />} />
            ) : (
              runs.slice(0, 5).map(run => (
                <button key={run.id} className="dashboard-row" onClick={() => navigate('/runs')}>
                  <div>
                    <strong>{run.task}</strong>
                    <small>{run.id} - {run.active_agent} - {run.duration_ms} ms</small>
                  </div>
                  <StatusBadge status={run.status} />
                </button>
              ))
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Knowledge Documents</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {documents.length === 0 ? (
              <EmptyState title="No backend documents" description="Use the + button in Workspace to upload documents." icon={<FileText className="h-5 w-5" />} />
            ) : (
              documents.slice(0, 5).map(document => (
                <button key={document.id} className="dashboard-row" onClick={() => navigate('/knowledge')}>
                  <div>
                    <strong>{document.name}</strong>
                    <small>{document.chunk_count} chunks - {formatBytes(document.size)}</small>
                  </div>
                  <ArrowRight className="h-4 w-4 text-surface-400" />
                </button>
              ))
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Generated Reports</CardTitle>
        </CardHeader>
        <CardContent>
          {reports.length === 0 ? (
            <EmptyState title="No reports generated" description="Completed backend chat runs will appear as report records here." icon={<BarChart3 className="h-5 w-5" />} />
          ) : (
            <div className="space-y-3">
              {reports.slice(0, 3).map(report => (
                <button key={report.id} className="dashboard-row" onClick={() => navigate('/reports')}>
                  <div>
                    <strong>{report.title}</strong>
                    <small>{report.active_agent} - {report.format}</small>
                  </div>
                  <StatusBadge status={report.status} />
                </button>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function MetricCard({ icon: Icon, label, value }: { icon: React.ElementType; label: string; value: number | string }) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-xs text-surface-500">{label}</p>
            <p className="text-2xl font-bold text-surface-900 dark:text-white">{value}</p>
          </div>
          <div className="metric-icon">
            <Icon className="h-5 w-5" />
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
