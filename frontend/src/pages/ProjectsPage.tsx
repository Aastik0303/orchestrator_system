import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import { FolderKanban, GitBranch, FileText, History, BarChart3, Clock, Play, Pause, Settings, ChevronRight } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { Button } from '@/components/ui/Button'
import { useAppStore } from '@/store/useAppStore'
import { cn } from '@/lib/utils'

export function ProjectsPage() {
  const navigate = useNavigate()
  const { projects } = useAppStore()
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid')

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-surface-900 dark:text-white">Projects</h2>
          <p className="mt-1 text-sm text-surface-500 dark:text-surface-400">
            Manage your isolated context workspaces
          </p>
        </div>
        <Button>
          <FolderKanban className="h-4 w-4" /> New Project
        </Button>
      </div>

      <div className={cn(
        'grid gap-4',
        viewMode === 'grid' ? 'sm:grid-cols-2 lg:grid-cols-3' : 'grid-cols-1'
      )}>
        {projects.map((project, index) => (
          <motion.div
            key={project.id}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: index * 0.1 }}
          >
            <Card hover className="cursor-pointer" onClick={() => navigate(`/projects/${project.id}`)}>
              <CardContent className="p-5">
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-3">
                    <div
                      className="flex h-10 w-10 items-center justify-center rounded-lg"
                      style={{ backgroundColor: project.color + '20' }}
                    >
                      <FolderKanban className="h-5 w-5" style={{ color: project.color }} />
                    </div>
                    <div>
                      <h3 className="font-semibold text-surface-900 dark:text-white">{project.name}</h3>
                      <p className="text-xs text-surface-500 dark:text-surface-400">{project.description}</p>
                    </div>
                  </div>
                  <StatusBadge status={project.status === 'active' ? 'completed' : project.status === 'paused' ? 'pending' : 'pending'} />
                </div>

                <div className="mt-4 grid grid-cols-3 gap-2 text-center">
                  <div className="rounded-lg bg-surface-50 p-2 dark:bg-surface-800">
                    <p className="text-lg font-bold text-surface-900 dark:text-white">{project.fileCount}</p>
                    <p className="text-[10px] text-surface-500">Files</p>
                  </div>
                  <div className="rounded-lg bg-surface-50 p-2 dark:bg-surface-800">
                    <p className="text-lg font-bold text-surface-900 dark:text-white">{project.workflowRuns}</p>
                    <p className="text-[10px] text-surface-500">Runs</p>
                  </div>
                  <div className="rounded-lg bg-surface-50 p-2 dark:bg-surface-800">
                    <p className="text-lg font-bold text-surface-900 dark:text-white">{project.reports}</p>
                    <p className="text-[10px] text-surface-500">Reports</p>
                  </div>
                </div>

                <div className="mt-3 flex items-center justify-between text-xs text-surface-500">
                  <span className="flex items-center gap-1">
                    <Clock className="h-3 w-3" /> {project.lastActivity}
                  </span>
                  {project.repositoryConnected && (
                    <span className="flex items-center gap-1">
                      <GitBranch className="h-3 w-3" /> Connected
                    </span>
                  )}
                </div>
              </CardContent>
            </Card>
          </motion.div>
        ))}
      </div>
    </div>
  )
}