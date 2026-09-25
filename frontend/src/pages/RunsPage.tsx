import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { History, Square } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { EmptyState } from '@/components/ui/EmptyState'
import { useRuntimeSnapshot } from '@/hooks/useRuntimeSnapshot'
import { getRun, getRunTrace, stopRun, type RuntimeRun, type TraceSpan } from '@/services/apiClient'

const filters = ['All', 'Completed', 'Failed', 'Running', 'Blocked', 'Cancelled', 'Timeout']

export function RunsPage() {
  const [activeFilter, setActiveFilter] = useState('All')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const { snapshot, error } = useRuntimeSnapshot()
  const runs = snapshot?.runs ?? []
  const filteredRuns = activeFilter === 'All' ? runs : runs.filter(run => run.status === activeFilter.toLowerCase())

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-2xl font-bold text-surface-900 dark:text-white">Runs and Logs</h2>
        <p className="mt-1 text-sm text-surface-500 dark:text-surface-400">Persisted runs with step states, failures and latency traces.</p>
      </div>

      {error && <EmptyState title="Could not load backend runs" description={error} icon={<History className="h-5 w-5" />} />}

      <div className="flex flex-wrap gap-2">
        {filters.map(filter => (
          <button
            key={filter}
            onClick={() => setActiveFilter(filter)}
            className={cn(
              'rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              activeFilter === filter
                ? 'bg-primary-100 text-primary-700 dark:bg-primary-900/30 dark:text-primary-400'
                : 'bg-surface-100 text-surface-600 hover:bg-surface-200 dark:bg-surface-800 dark:text-surface-400'
            )}
          >
            {filter}
          </button>
        ))}
      </div>

      <Card>
        {filteredRuns.length === 0 ? (
          <EmptyState title="No runs yet" description="Submit a task from Workspace to populate this table from the backend." icon={<History className="h-5 w-5" />} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-surface-200 dark:border-surface-800">
                  <th className="px-4 py-3 font-medium text-surface-500">Run ID</th>
                  <th className="px-4 py-3 font-medium text-surface-500">Task</th>
                  <th className="px-4 py-3 font-medium text-surface-500">Agent</th>
                  <th className="px-4 py-3 font-medium text-surface-500">Status</th>
                  <th className="px-4 py-3 font-medium text-surface-500">Duration</th>
                  <th className="px-4 py-3 font-medium text-surface-500">Tokens</th>
                  <th className="px-4 py-3 font-medium text-surface-500">Response</th>
                </tr>
              </thead>
              <tbody>
                {filteredRuns.map((run, index) => (
                  <motion.tr
                    key={run.id}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ delay: index * 0.03 }}
                    onClick={() => setSelectedId(run.id)}
                    className={cn(
                      'cursor-pointer border-b border-surface-100 hover:bg-surface-50 dark:border-surface-800 dark:hover:bg-surface-800/50',
                      selectedId === run.id && 'bg-surface-50 dark:bg-surface-800/50'
                    )}
                  >
                    <td className="px-4 py-3 font-mono text-xs text-surface-600 dark:text-surface-400">{run.id}</td>
                    <td className="px-4 py-3 font-medium text-surface-900 dark:text-white">{run.task.slice(0, 80)}</td>
                    <td className="px-4 py-3 text-surface-600 dark:text-surface-400">{run.active_agent ?? '-'}</td>
                    <td className="px-4 py-3"><StatusBadge status={run.status} /></td>
                    <td className="px-4 py-3 text-surface-600 dark:text-surface-400">{run.duration_ms} ms</td>
                    <td className="px-4 py-3 text-surface-600 dark:text-surface-400">{run.metrics?.tokens ?? '-'}</td>
                    <td className="px-4 py-3 text-surface-600 dark:text-surface-400">{run.response.slice(0, 100)}</td>
                  </motion.tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {selectedId && <RunDetail runId={selectedId} />}
    </div>
  )
}

function RunDetail({ runId }: { runId: string }) {
  const [run, setRun] = useState<RuntimeRun | null>(null)
  const [spans, setSpans] = useState<TraceSpan[]>([])
  const [error, setError] = useState<string | null>(null)
  const [stopping, setStopping] = useState(false)

  useEffect(() => {
    let active = true
    Promise.all([getRun(runId), getRunTrace(runId)])
      .then(([detail, trace]) => {
        if (!active) return
        setRun(detail)
        setSpans(trace.spans)
        setError(null)
      })
      .catch(caught => active && setError(caught instanceof Error ? caught.message : 'Could not load run.'))
    return () => {
      active = false
    }
  }, [runId, stopping])

  async function handleStop() {
    setStopping(true)
    try {
      await stopRun(runId)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not stop run.')
    } finally {
      setStopping(false)
    }
  }

  if (error) return <EmptyState title="Run detail unavailable" description={error} icon={<History className="h-5 w-5" />} />
  if (!run) return null

  const topSpans = [...spans]
    .filter(span => span.kind !== 'api')
    .sort((left, right) => right.latency_ms - left.latency_ms)
    .slice(0, 8)
  const metrics = run.metrics ?? {}

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Steps</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-3 text-xs text-surface-500">
            <span>trace {run.trace_id ?? '-'}</span>
            <span>{metrics.llm_calls ?? 0} LLM calls</span>
            <span>{metrics.tool_calls ?? 0} tool calls</span>
            <span>{metrics.retries ?? 0} retries</span>
            {(run.status === 'running' || run.status === 'queued') && (
              <button onClick={handleStop} disabled={stopping} className="inline-flex items-center gap-1 rounded-md bg-surface-100 px-2 py-1 font-medium text-surface-700 dark:bg-surface-800 dark:text-surface-200">
                <Square className="h-3 w-3" /> Stop
              </button>
            )}
          </div>
          {(run.steps ?? []).length === 0 && <p className="text-sm text-surface-500">No agent steps (the request may have been blocked by a guardrail).</p>}
          {(run.steps ?? []).map(step => (
            <div key={step.step_id} className="rounded-lg border border-surface-200 p-3 dark:border-surface-800">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-xs">{step.step_id}</span>
                <StatusBadge status={step.status} />
              </div>
              <div className="mt-1 text-xs text-surface-500">
                {step.agent} · {step.attempts} attempt{step.attempts === 1 ? '' : 's'} · {step.latency_ms ?? 0} ms
                {step.depends_on.length > 0 && <> · after {step.depends_on.join(', ')}</>}
              </div>
              {step.error && (
                <div className="mt-2 text-xs text-red-600 dark:text-red-400">
                  {step.error.error_type}{step.error.retryable ? ' (retryable)' : ''}: {step.error.message}
                </div>
              )}
            </div>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Latency breakdown</CardTitle>
        </CardHeader>
        <CardContent>
          {topSpans.length === 0 ? (
            <p className="text-sm text-surface-500">No trace spans recorded.</p>
          ) : (
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="text-surface-500">
                  <th className="py-1">Kind</th>
                  <th className="py-1">Name</th>
                  <th className="py-1 text-right">ms</th>
                  <th className="py-1 text-right">Tokens</th>
                </tr>
              </thead>
              <tbody>
                {topSpans.map(span => (
                  <tr key={span.span_id} className="border-t border-surface-100 dark:border-surface-800">
                    <td className="py-1">{span.kind}</td>
                    <td className="py-1 font-mono">{span.name}</td>
                    <td className="py-1 text-right tabular-nums">{span.latency_ms.toFixed(1)}</td>
                    <td className="py-1 text-right tabular-nums">{String(span.attributes.total_tokens ?? '')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function cn(...classes: (string | undefined | false)[]) {
  return classes.filter(Boolean).join(' ')
}
