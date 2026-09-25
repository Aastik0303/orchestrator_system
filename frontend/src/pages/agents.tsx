import { useEffect, useState } from 'react'
import { Bot, Brain, Gauge, Server, ShieldCheck, Wrench } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { EmptyState } from '@/components/ui/EmptyState'
import { getAgentCapabilities, type AgentCapability } from '@/services/apiClient'

const groups: Array<{ id: AgentCapability['category']; title: string; icon: typeof Bot }> = [
  { id: 'system', title: 'System Agents', icon: Brain },
  { id: 'task', title: 'Task Agents', icon: Bot },
  { id: 'output', title: 'Output and Quality Agents', icon: ShieldCheck },
]

export function AgentsPage() {
  const [agents, setAgents] = useState<AgentCapability[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getAgentCapabilities()
      .then(result => {
        setAgents(result.agents)
        setError(null)
      })
      .catch(caught => setError(caught instanceof Error ? caught.message : 'Could not load agents.'))
  }, [])

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-2xl font-bold text-surface-900 dark:text-white">Agents</h2>
        <p className="mt-1 text-sm text-surface-500 dark:text-surface-400">Backend agent registry and measured runtime activity.</p>
      </div>

      {error && <EmptyState title="Could not load backend capabilities" description={error} icon={<Server className="h-5 w-5" />} />}

      {groups.map(group => {
        const GroupIcon = group.icon
        const groupedAgents = agents.filter(agent => agent.category === group.id)
        if (!groupedAgents.length) return null
        return (
          <section key={group.id} className="agent-capability-section">
            <div className="capability-section-heading">
              <GroupIcon className="h-5 w-5" />
              <h3>{group.title}</h3>
            </div>
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {groupedAgents.map(agent => (
                <Card key={agent.id} hover>
                  <CardContent className="p-5">
                    <div className="capability-card-heading">
                      <div className="capability-icon"><GroupIcon className="h-5 w-5" /></div>
                      <div>
                        <h4>{agent.name}</h4>
                        <span>{agent.user_selectable ? 'User selectable' : 'System managed'}</span>
                      </div>
                      <Badge variant={agent.status === 'available' ? 'success' : 'warning'}>{agent.status}</Badge>
                    </div>
                    <p className="capability-description">{agent.description}</p>
                    <dl className="capability-metrics">
                      <div><dt>Model</dt><dd>{agent.model}</dd></div>
                      <div><dt>Capabilities</dt><dd>{(agent.capabilities ?? []).join(', ') || '-'}</dd></div>
                      <div><dt>Timeout</dt><dd>{agent.timeout_seconds ?? '-'} s</dd></div>
                      <div><dt>Retries</dt><dd>{agent.retry_policy?.max_retries ?? 0}</dd></div>
                      <div><dt>Token budget</dt><dd>{agent.token_budget ?? 0}</dd></div>
                      <div><dt>Permissions</dt><dd>{(agent.permissions ?? []).join(', ') || 'none'}</dd></div>
                      <div><dt>Memory</dt><dd>{agent.memory_access}</dd></div>
                      <div><dt>Runs</dt><dd>{agent.run_count}</dd></div>
                      <div><dt>Success</dt><dd>{agent.success_rate == null ? 'No data' : `${agent.success_rate}%`}</dd></div>
                      <div><dt>Latency</dt><dd>{agent.average_latency_ms == null ? 'No data' : `${agent.average_latency_ms} ms`}</dd></div>
                    </dl>
                    <div className="capability-tools">
                      <Wrench className="h-4 w-4" />
                      <span>{agent.tools.length ? agent.tools.join(', ') : 'No external tools'}</span>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          </section>
        )
      })}

      {!error && agents.length === 0 && (
        <Card><CardContent><EmptyState title="No backend agents reported" description="Start the backend to load the agent registry." icon={<Gauge className="h-5 w-5" />} /></CardContent></Card>
      )}
    </div>
  )
}
