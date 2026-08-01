import { useParams } from 'react-router-dom'
import { FolderKanban, GitBranch, FileText, History, BarChart3, Settings, ArrowLeft } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Tabs, TabList, Tab, TabPanel } from '@/components/ui/Tabs'
import { Button } from '@/components/ui/Button'
import { useAppStore } from '@/store/useAppStore'
import { useNavigate } from 'react-router-dom'

export function ProjectDetailPage() {
  const { projectId } = useParams()
  const navigate = useNavigate()
  const { projects } = useAppStore()
  const project = projects.find(p => p.id === projectId)

  if (!project) return <div className="p-6">Project not found</div>

  const tabs = ['Overview', 'AI Tasks', 'Files', 'Knowledge', 'Runs', 'Reports', 'Memory', 'Settings']

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center gap-3">
        <button onClick={() => navigate('/projects')} className="flex h-8 w-8 items-center justify-center rounded-lg text-surface-500 hover:bg-surface-100 dark:hover:bg-surface-800">
          <ArrowLeft className="h-4 w-4" />
        </button>
        <div>
          <h2 className="text-2xl font-bold text-surface-900 dark:text-white">{project.name}</h2>
          <p className="text-sm text-surface-500 dark:text-surface-400">{project.description}</p>
        </div>
      </div>

      <Tabs defaultTab="Overview">
        <TabList>
          {tabs.map(tab => <Tab key={tab} id={tab}>{tab}</Tab>)}
        </TabList>
        <TabPanel id="Overview">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Card>
              <CardContent className="p-4">
                <p className="text-2xl font-bold">{project.fileCount}</p>
                <p className="text-xs text-surface-500">Files</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-2xl font-bold">{project.workflowRuns}</p>
                <p className="text-xs text-surface-500">Workflow Runs</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-2xl font-bold">{project.reports}</p>
                <p className="text-xs text-surface-500">Reports</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <p className="text-2xl font-bold">{project.lastActivity}</p>
                <p className="text-xs text-surface-500">Last Activity</p>
              </CardContent>
            </Card>
          </div>
        </TabPanel>
        {tabs.slice(1).map(tab => (
          <TabPanel key={tab} id={tab}>
            <div className="py-8 text-center text-surface-500">{tab} content coming soon</div>
          </TabPanel>
        ))}
      </Tabs>
    </div>
  )
}