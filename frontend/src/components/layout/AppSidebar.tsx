import { BarChart3, BookOpen, Bot, History, Home, Settings, FileText, MessageSquare, Wrench } from 'lucide-react'
import { useLocation, useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { useAppStore } from '@/store/useAppStore'

const navItems = [
  { path: '/', label: 'Dashboard', icon: Home },
  { path: '/workspace', label: 'Workspace', icon: MessageSquare },
  { path: '/knowledge', label: 'Knowledge', icon: BookOpen },
  { path: '/agents', label: 'Agents', icon: Bot },
  { path: '/tools', label: 'Tools', icon: Wrench },
  { path: '/runs', label: 'Runs', icon: History },
  { path: '/reports', label: 'Reports', icon: FileText },
  { path: '/evaluations', label: 'Evaluations', icon: BarChart3 },
  { path: '/settings', label: 'Settings', icon: Settings },
]

export function AppSidebar() {
  const navigate = useNavigate()
  const location = useLocation()
  const { leftSidebarOpen } = useAppStore()

  return (
    <aside className={cn('app-sidebar', !leftSidebarOpen && 'app-sidebar-collapsed')}>
      <div className="app-brand">
        <div className="app-brand-mark">AI</div>
        {leftSidebarOpen && (
          <div>
            <h1>AI Builder Studio</h1>
            <p>Agent workflow builder</p>
          </div>
        )}
      </div>
      <nav className="app-nav">
        {navItems.map(item => {
          const Icon = item.icon
          const active = item.path === '/' ? location.pathname === '/' : location.pathname.startsWith(item.path)
          return (
            <button
              key={item.path}
              className={cn('app-nav-item', active && 'app-nav-item-active')}
              onClick={() => navigate(item.path)}
              title={item.label}
            >
              <Icon className="h-4 w-4" />
              {leftSidebarOpen && <span>{item.label}</span>}
            </button>
          )
        })}
      </nav>
    </aside>
  )
}
