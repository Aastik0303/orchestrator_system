import { CheckCircle, Clock, GitBranch } from 'lucide-react'
import { useAppStore } from '@/store/useAppStore'

export function ContextSidebar() {
  const { rightSidebarOpen, projects } = useAppStore()
  if (!rightSidebarOpen) return null

  return (
    <aside className="context-sidebar">
      <h3>Context</h3>
      <div className="context-section">
        <p className="context-label">Active Projects</p>
        {projects.slice(0, 3).map(project => (
          <div key={project.id} className="context-item">
            <span style={{ backgroundColor: project.color }} />
            <div>
              <strong>{project.name}</strong>
              <small>{project.lastActivity}</small>
            </div>
          </div>
        ))}
      </div>
      <div className="context-section">
        <p className="context-label">System</p>
        <div className="context-pill"><CheckCircle className="h-4 w-4" /> Frontend running locally</div>
        <div className="context-pill"><GitBranch className="h-4 w-4" /> Demo data loaded</div>
        <div className="context-pill"><Clock className="h-4 w-4" /> Ready for backend wiring</div>
      </div>
    </aside>
  )
}
