import { motion } from 'framer-motion'
import { ArrowRight, FileText } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { EmptyState } from '@/components/ui/EmptyState'
import { useRuntimeSnapshot } from '@/hooks/useRuntimeSnapshot'

export function ReportsPage() {
  const { snapshot, error } = useRuntimeSnapshot()
  const reports = snapshot?.reports ?? []

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-2xl font-bold text-surface-900 dark:text-white">Reports</h2>
        <p className="mt-1 text-sm text-surface-500 dark:text-surface-400">Generated from completed backend runs only.</p>
      </div>

      {error && <EmptyState title="Could not load reports" description={error} icon={<FileText className="h-5 w-5" />} />}

      {reports.length === 0 ? (
        <Card>
          <CardContent>
            <EmptyState title="No real reports yet" description="A completed backend run creates a report preview here." icon={<FileText className="h-5 w-5" />} />
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {reports.map((report, index) => (
            <motion.div
              key={report.id}
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: index * 0.05 }}
            >
              <Card hover>
                <CardContent className="p-5">
                  <div className="flex items-start justify-between">
                    <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-indigo-50 dark:bg-indigo-900/20">
                      <FileText className="h-5 w-5 text-indigo-600 dark:text-indigo-400" />
                    </div>
                    <Badge variant={report.status === 'completed' ? 'success' : 'neutral'}>{report.format.toUpperCase()}</Badge>
                  </div>

                  <h3 className="mt-3 font-semibold text-surface-900 dark:text-white">{report.title}</h3>
                  <p className="text-xs text-surface-500">{report.run_id} - {report.active_agent}</p>
                  <p className="mt-3 text-sm text-surface-600 dark:text-surface-400">{report.content.slice(0, 180)}</p>

                  <div className="mt-4 flex items-center justify-between">
                    <span className="text-sm font-medium text-primary-600">Backend generated</span>
                    <button className="flex h-8 w-8 items-center justify-center rounded-md text-surface-400 hover:bg-surface-100 dark:hover:bg-surface-800">
                      <ArrowRight className="h-4 w-4" />
                    </button>
                  </div>
                </CardContent>
              </Card>
            </motion.div>
          ))}
        </div>
      )}
    </div>
  )
}
