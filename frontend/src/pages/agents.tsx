import { motion } from 'framer-motion'
import { Bot, Server } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { EmptyState } from '@/components/ui/EmptyState'
import { useRuntimeSnapshot } from '@/hooks/useRuntimeSnapshot'

export function AgentsPage() {
  const { snapshot, error } = useRuntimeSnapshot()
  const capabilities = snapshot?.capabilities ?? []

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-2xl font-bold text-surface-900 dark:text-white">Agents</h2>
        <p className="mt-1 text-sm text-surface-500 dark:text-surface-400">Actual agents and services exposed by the running backend.</p>
      </div>

      {error && <EmptyState title="Could not load backend capabilities" description={error} icon={<Server className="h-5 w-5" />} />}

      {capabilities.length === 0 ? (
        <Card>
          <CardContent>
            <EmptyState title="No backend agents reported" description="Start the backend to load available agents." icon={<Bot className="h-5 w-5" />} />
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {capabilities.map((capability, index) => (
            <motion.div
              key={capability.id}
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: index * 0.05 }}
            >
              <Card hover>
                <CardContent className="p-5">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-green-50 dark:bg-green-900/20">
                        <Bot className="h-5 w-5 text-green-600" />
                      </div>
                      <div>
                        <h3 className="font-semibold text-surface-900 dark:text-white">{capability.name}</h3>
                        <p className="text-xs text-surface-500">{capability.id}</p>
                      </div>
                    </div>
                    <Badge variant="success">{capability.status}</Badge>
                  </div>

                  <div className="mt-4 text-xs text-surface-500">
                    Loaded from backend runtime capability registry.
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
