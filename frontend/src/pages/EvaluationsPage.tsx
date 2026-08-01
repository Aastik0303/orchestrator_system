import { AlertTriangle, BarChart3, CheckCircle, Clock } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { ProgressBar } from '@/components/ui/ProgressBar'
import { EmptyState } from '@/components/ui/EmptyState'
import { useRuntimeSnapshot } from '@/hooks/useRuntimeSnapshot'

export function EvaluationsPage() {
  const { snapshot, error } = useRuntimeSnapshot()
  const evaluation = snapshot?.evaluation

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-2xl font-bold text-surface-900 dark:text-white">Evaluations</h2>
        <p className="mt-1 text-sm text-surface-500 dark:text-surface-400">Live aggregate quality signals from backend runs.</p>
      </div>

      {error && <EmptyState title="Could not load evaluation data" description={error} icon={<BarChart3 className="h-5 w-5" />} />}

      {!evaluation?.available ? (
        <Card>
          <CardContent>
            <EmptyState title="No real evaluation data yet" description="Run a task from Workspace to create backend success and latency metrics." icon={<BarChart3 className="h-5 w-5" />} />
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Backend Run Quality</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <Metric label="Success Rate" value={evaluation.success_rate} />
                <Metric label="Completed Runs" value={evaluation.successful_runs} max={Math.max(1, evaluation.successful_runs + evaluation.failed_runs)} />
                <Metric label="Failed Runs" value={evaluation.failed_runs} max={Math.max(1, evaluation.successful_runs + evaluation.failed_runs)} />
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Runtime Signals</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="evaluation-signal">
                  <Clock className="h-5 w-5" />
                  <div>
                    <strong>{evaluation.average_latency_ms} ms</strong>
                    <span>Average backend latency</span>
                  </div>
                </div>
                <div className="evaluation-signal">
                  <CheckCircle className="h-5 w-5" />
                  <div>
                    <strong>{evaluation.successful_runs}</strong>
                    <span>Successful backend responses</span>
                  </div>
                </div>
                <div className="evaluation-signal">
                  <AlertTriangle className="h-5 w-5" />
                  <div>
                    <strong>{evaluation.failed_runs}</strong>
                    <span>Fallback or failed responses</span>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
        </>
      )}
    </div>
  )
}

function Metric({ label, value, max = 100 }: { label: string; value: number; max?: number }) {
  const displayValue = max === 100 ? `${value}%` : value
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-sm font-medium text-surface-700 dark:text-surface-300">{label}</span>
        <span className="text-sm font-bold text-surface-900 dark:text-white">{displayValue}</span>
      </div>
      <ProgressBar value={value} max={max} size="sm" />
    </div>
  )
}
