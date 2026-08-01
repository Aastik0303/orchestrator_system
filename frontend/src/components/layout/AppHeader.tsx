import { BarChart3, Bell, BookOpen, Bot, FileText, History, Home, MessageSquare, Moon, Play, Settings, Sun } from 'lucide-react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAppStore } from '@/store/useAppStore'
import { cn } from '@/lib/utils'

const navItems = [
  { path: '/', label: 'Dashboard', icon: Home },
  { path: '/workspace', label: 'Workspace', icon: MessageSquare },
  { path: '/knowledge', label: 'Knowledge', icon: BookOpen },
  { path: '/agents', label: 'Agents', icon: Bot },
  { path: '/runs', label: 'Runs', icon: History },
  { path: '/reports', label: 'Reports', icon: FileText },
  { path: '/evaluations', label: 'Evaluations', icon: BarChart3 },
  { path: '/settings', label: 'Settings', icon: Settings },
]

export function AppHeader() {
  const location = useLocation()
  const navigate = useNavigate()
  const { darkMode, toggleDarkMode } = useAppStore()

  return (
    <header className="app-header">
      <button className="topbar-brand" onClick={() => navigate('/workspace')}>
        <span className="app-brand-mark">AI</span>
        <div>
          <strong>AI Builder Studio</strong>
          <small>Agent workflow builder</small>
        </div>
      </button>

      <nav className="topbar-nav" aria-label="Primary navigation">
        {navItems.map(item => {
          const Icon = item.icon
          const active = item.path === '/' ? location.pathname === '/' : location.pathname.startsWith(item.path)
          return (
            <button
              key={item.path}
              className={cn('topbar-nav-item', active && 'topbar-nav-item-active')}
              onClick={() => navigate(item.path)}
            >
              <Icon className="h-4 w-4" />
              <span>{item.label}</span>
            </button>
          )
        })}
      </nav>

      <div className="topbar-actions">
        <button className="topbar-run-button" title="Run workflow">
          <Play className="h-4 w-4" />
          <span>Run</span>
        </button>
        <button className="header-icon-button" title="Notifications">
          <Bell className="h-5 w-5" />
        </button>
        <button className="header-icon-button" onClick={toggleDarkMode} title="Toggle theme">
          {darkMode ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
        </button>
      </div>
    </header>
  )
}
