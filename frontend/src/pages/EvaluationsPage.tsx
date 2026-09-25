import { useEffect, useState } from 'react'
import { AlertTriangle, BarChart3, CheckCircle, Clock, ShieldCheck } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { ProgressBar } from '@/components/ui/ProgressBar'
import { EmptyState } from '@/components/ui/EmptyState'
import { Badge } from '@/components/ui/Badge'
import { useRuntimeSnapshot } from '@/hooks/useRuntimeSnapshot'
import { getLatencyMetrics, getLatestEvalSuite, type EvalSuiteReport, type LatencyContributor } from '@/services/apiClient'

export function EvaluationsPage() {
  const { snapshot, error } = useRuntimeSnapshot()
  const evaluation = snapshot?.evaluation
  const [contributors, setContributors] = useState<LatencyContributor[]>([])
  const [suite, setSuite] = useState<EvalSuiteReport | null>(null)
  const [suiteError, setSuiteError] = useState<string | null>(null)

  useEffect(() => {
    getLatencyMetrics().then(result => setContributors(result.top_contributors)).catch(() => setContributors([]))
    getLatestEvalSuite()
      .then(result => {
        setSuite(result)
        setSuiteError(null)
      })
      .catch(caught => setSuiteError(caught instanceof Error ? caught.message : 'No suite results.'))
  }, [])

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-2xl font-bold text-surface-900 dark:text-white">Evaluations</h2>
        <p className="mt-1 text-sm text-surface-500 dark:text-surface-400">Live run quality, latency contributors and offline evaluation suites.</p>
      </div>

      {error && <EmptyState title="Could not load evaluation data" description={error} icon={<BarChart3 className="h-5 w-5" />} />}

      {!evaluation?.available ? (
        <Card>
          <CardContent>
            <EmptyState title="No run data yet" description="Run a task from Workspace to create backend success and latency metrics." icon={<BarChart3 className="h-5 w-5" />} />
          </CardContent>
        </Card>
      ) : (
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
              <Signal icon={<Clock className="h-5 w-5" />} value={`${evaluation.average_latency_ms} ms`} label="Average run latency" />
              <Signal icon={<CheckCircle className="h-5 w-5" />} value={evaluation.successful_runs} label="Successful runs" />
              <Signal icon={<AlertTriangle className="h-5 w-5" />} value={evaluation.failed_runs} label="Failed or timed-out runs" />
              <Signal icon={<ShieldCheck className="h-5 w-5" />} value={evaluation.blocked_runs ?? 0} label="Blocked by guardrails or approvals" />
            </CardContent>
          </Card>
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Top latency contributors</CardTitle>
        </CardHeader>
        <CardContent>
          {contributors.length === 0 ? (
            <p className="text-sm text-surface-500">No trace data yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="text-surface-500">
                    <th className="py-1">Kind</th>
                    <th className="py-1">Name</th>
                    <th className="py-1 text-right">Calls</th>
                    <th className="py-1 text-right">Total ms</th>
                    <th className="py-1 text-right">p50</th>
                    <th className="py-1 text-right">p95</th>
                    <th className="py-1 text-right">Errors</th>
                    <th className="py-1 text-right">Tokens</th>
                  </tr>
                </thead>
                <tbody>
                  {contributors.map(item => (
                    <tr key={`${item.kind}:${item.name}`} className="border-t border-surface-100 dark:border-surface-800">
                      <td className="py-1">{item.kind}</td>
                      <td className="py-1 font-mono">{item.name}</td>
                      <td className="py-1 text-right tabular-nums">{item.count}</td>
                      <td className="py-1 text-right tabular-nums">{item.total_ms.toFixed(0)}</td>
                      <td className="py-1 text-right tabular-nums">{item.p50_ms.toFixed(1)}</td>
                      <td className="py-1 text-right tabular-nums">{item.p95_ms.toFixed(1)}</td>
                      <td className="py-1 text-right tabular-nums">{(item.error_rate * 100).toFixed(0)}%</td>
                      <td className="py-1 text-right tabular-nums">{item.tokens}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Offline evaluation suites</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {suiteError || !suite ? (
            <p className="text-sm text-surface-500">{suiteError ?? 'Loading…'} Run <code>python -m evals.run</code> in the backend.</p>
          ) : (
            <>
              <div className="flex items-center gap-2 text-sm">
                <Badge variant={suite.passed ? 'success' : 'danger'}>{suite.passed ? 'All thresholds met' : 'Threshold failures'}</Badge>
                <span className="text-surface-500">recorded {new Date(suite.recorded_at).toLocaleString()}</span>
              </div>
              {suite.failures.map(failure => <p key={failure} className="text-xs text-red-600">{failure}</p>)}
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                {Object.entries(suite.summary).map(([name, metrics]) => (
                  <div key={name} className="rounded-lg border border-surface-200 p-3 dark:border-surface-800">
                    <h4 className="mb-2 text-sm font-semibold capitalize">{name}</h4>
                    <dl className="space-y-1 text-xs">
                      {Object.entries(metrics)
                        .filter(([, value]) => typeof value === 'number' || typeof value === 'string')
                        .map(([key, value]) => (
                          <div key={key} className="flex justify-between gap-2">
                            <dt className="text-surface-500">{key.replace(/_/g, ' ')}</dt>
                            <dd className="font-medium tabular-nums">{String(value)}</dd>
                          </div>
                        ))}
                    </dl>
                  </div>
                ))}
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function Signal({ icon, value, label }: { icon: React.ReactNode; value: React.ReactNode; label: string }) {
  return (
    <div className="evaluation-signal">
      {icon}
      <div>
        <strong>{value}</strong>
        <span>{label}</span>
      </div>
    </div>
  )
}

function Metric({ label, value, max = 100 }: { label: string; value: number; max?: number }) {
  const displayValue = max === 100 ? `${value}%` : value
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-sm font-medium text-surface-700 dark:text-surface-300">{label}</span>
        <span className="text-sm font-bold text-surface-900 dark:text-white">{displayValue}</span>
      </div>
      <ProgressBar value={value} max={max} size="sm" />
    </div>
  )
}
