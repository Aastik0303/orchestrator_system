import { useState } from 'react'
import { motion } from 'framer-motion'
import { History } from 'lucide-react'
import { Card } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { EmptyState } from '@/components/ui/EmptyState'
import { useRuntimeSnapshot } from '@/hooks/useRuntimeSnapshot'

const filters = ['All', 'Completed', 'Failed', 'Running']

export function RunsPage() {
  const [activeFilter, setActiveFilter] = useState('All')
  const { snapshot, error } = useRuntimeSnapshot()
  const runs = snapshot?.runs ?? []
  const filteredRuns = activeFilter === 'All' ? runs : runs.filter(run => run.status === activeFilter.toLowerCase())

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-2xl font-bold text-surface-900 dark:text-white">Runs and Logs</h2>
        <p className="mt-1 text-sm text-surface-500 dark:text-surface-400">Real backend chat and orchestration history.</p>
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
          <EmptyState title="No real runs yet" description="Submit a task from Workspace to populate this table from the backend." icon={<History className="h-5 w-5" />} />
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
                  <th className="px-4 py-3 font-medium text-surface-500">Files</th>
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
                    className="border-b border-surface-100 hover:bg-surface-50 dark:border-surface-800 dark:hover:bg-surface-800/50"
                  >
                    <td className="px-4 py-3 font-mono text-xs text-surface-600 dark:text-surface-400">{run.id}</td>
                    <td className="px-4 py-3 font-medium text-surface-900 dark:text-white">{run.task}</td>
                    <td className="px-4 py-3 text-surface-600 dark:text-surface-400">{run.active_agent}</td>
                    <td className="px-4 py-3"><StatusBadge status={run.status} /></td>
                    <td className="px-4 py-3 text-surface-600 dark:text-surface-400">{run.duration_ms} ms</td>
                    <td className="px-4 py-3 text-surface-600 dark:text-surface-400">{run.file_count}</td>
                    <td className="px-4 py-3 text-surface-600 dark:text-surface-400">{run.response.slice(0, 100)}</td>
                  </motion.tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}

function cn(...classes: (string | undefined | false)[]) {
  return classes.filter(Boolean).join(' ')
}
