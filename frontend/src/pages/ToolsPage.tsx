import { useEffect, useState } from 'react'
import { Activity, CheckCircle, Clock, Plug, RefreshCw, Server, ShieldCheck, Wrench } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { EmptyState } from '@/components/ui/EmptyState'
import { getMcpCapabilities, testMcpConnection, type McpServerCapability } from '@/services/apiClient'

export function ToolsPage() {
  const [servers, setServers] = useState<McpServerCapability[]>([])
  const [testingServer, setTestingServer] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function loadServers() {
    try {
      const result = await getMcpCapabilities()
      setServers(result.servers)
      setError(null)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not load MCP servers.')
    }
  }

  useEffect(() => { loadServers() }, [])

  async function handleTest(serverId: string) {
    setTestingServer(serverId)
    try {
      await testMcpConnection(serverId)
      await loadServers()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Connection test failed.')
    } finally {
      setTestingServer(null)
    }
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-2xl font-bold text-surface-900 dark:text-white">MCP Tools</h2>
        <p className="mt-1 text-sm text-surface-500 dark:text-surface-400">Registered servers, enforced permissions, and measured tool activity.</p>
      </div>

      {error && <EmptyState title="Could not load MCP registry" description={error} icon={<Server className="h-5 w-5" />} />}

      <div className="mcp-server-list">
        {servers.map(server => (
          <section key={server.id} className="mcp-server-section">
            <div className="mcp-server-header">
              <div className="capability-section-heading">
                <Plug className="h-5 w-5" />
                <div><h3>{server.name}</h3><span>{server.id}</span></div>
              </div>
              <div className="mcp-server-actions">
                <Badge variant={server.status === 'connected' ? 'success' : 'neutral'}>{server.status.replace(/_/g, ' ')}</Badge>
                <button className="mcp-test-button" onClick={() => handleTest(server.id)} disabled={testingServer === server.id} title="Test connection">
                  <RefreshCw className={`h-4 w-4 ${testingServer === server.id ? 'spin-icon' : ''}`} />
                  Test
                </button>
              </div>
            </div>
            <div className="mcp-tool-grid">
              {server.tools.map(tool => (
                <Card key={`${server.id}-${tool.name}`}>
                  <CardContent className="p-4">
                    <div className="mcp-tool-heading">
                      <div className="capability-icon"><Wrench className="h-4 w-4" /></div>
                      <div><h4>{tool.name}</h4><span>{tool.status.replace(/_/g, ' ')}</span></div>
                      <Badge variant={tool.permission === 'allowed' ? 'success' : 'warning'}>{tool.permission.replace(/_/g, ' ')}</Badge>
                    </div>
                    <p className="capability-description">{tool.description}</p>
                    <div className="mcp-tool-stats">
                      <span><Clock className="h-3 w-3" />{tool.average_latency_ms == null ? 'No latency data' : `${tool.average_latency_ms} ms`}</span>
                      <span><Activity className="h-3 w-3" />{tool.success_rate == null ? 'No run data' : `${tool.success_rate}% success`}</span>
                      <span>{tool.permission === 'approval_required' ? <ShieldCheck className="h-3 w-3" /> : <CheckCircle className="h-3 w-3" />}{tool.last_used ? new Date(tool.last_used).toLocaleString() : 'Never used'}</span>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          </section>
        ))}
      </div>

      {!error && servers.length === 0 && <EmptyState title="No MCP servers registered" description="The backend registry returned no servers." icon={<Plug className="h-5 w-5" />} />}
    </div>
  )
}
