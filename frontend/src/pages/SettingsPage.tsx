import { useState } from 'react'
import { motion } from 'framer-motion'
import { User, Palette, Brain, Shield, Bell, CreditCard, Key, Save } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { useAppStore } from '@/store/useAppStore'

const sections = [
  { id: 'profile', label: 'Profile', icon: User },
  { id: 'appearance', label: 'Appearance', icon: Palette },
  { id: 'ai', label: 'AI Models', icon: Brain },
  { id: 'memory', label: 'Memory', icon: Brain },
  { id: 'privacy', label: 'Data Privacy', icon: Shield },
  { id: 'notifications', label: 'Notifications', icon: Bell },
  { id: 'billing', label: 'Usage and Billing', icon: CreditCard },
  { id: 'api', label: 'API Keys', icon: Key },
]

export function SettingsPage() {
  const { settings, updateSettings, darkMode, toggleDarkMode } = useAppStore()
  const [activeSection, setActiveSection] = useState('profile')

  return (
    <div className="flex h-[calc(100vh-3.5rem)]">
      {/* Sidebar */}
      <div className="w-64 border-r border-surface-200 bg-surface-50 p-4 dark:border-surface-800 dark:bg-surface-900/50">
        <h3 className="mb-4 text-xs font-semibold uppercase tracking-wider text-surface-400">Settings</h3>
        <div className="space-y-0.5">
          {sections.map(section => {
            const Icon = section.icon
            return (
              <button
                key={section.id}
                onClick={() => setActiveSection(section.id)}
                className={cn(
                  'flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                  activeSection === section.id
                    ? 'bg-white text-primary-700 shadow-sm dark:bg-surface-800 dark:text-primary-400'
                    : 'text-surface-600 hover:bg-white/50 dark:text-surface-400 dark:hover:bg-surface-800/50'
                )}
              >
                <Icon className="h-4 w-4" />
                {section.label}
              </button>
            )
          })}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-6">
        <motion.div
          key={activeSection}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
        >
          {activeSection === 'profile' && (
            <div className="max-w-2xl space-y-6">
              <h2 className="text-xl font-bold text-surface-900 dark:text-white">Profile</h2>
              <Card>
                <CardContent className="space-y-4 p-5">
                  <div>
                    <label className="block text-sm font-medium text-surface-700 dark:text-surface-300">Name</label>
                    <input type="text" defaultValue="Aastik" className="mt-1 w-full rounded-lg border border-surface-300 px-3 py-2 text-sm dark:border-surface-700 dark:bg-surface-800" />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-surface-700 dark:text-surface-300">Email</label>
                    <input type="email" defaultValue="aastik@example.com" className="mt-1 w-full rounded-lg border border-surface-300 px-3 py-2 text-sm dark:border-surface-700 dark:bg-surface-800" />
                  </div>
                </CardContent>
              </Card>
            </div>
          )}

          {activeSection === 'appearance' && (
            <div className="max-w-2xl space-y-6">
              <h2 className="text-xl font-bold text-surface-900 dark:text-white">Appearance</h2>
              <Card>
                <CardContent className="space-y-4 p-5">
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="font-medium text-surface-900 dark:text-white">Dark Mode</p>
                      <p className="text-xs text-surface-500">Toggle between light and dark themes</p>
                    </div>
                    <button
                      onClick={toggleDarkMode}
                      className={cn(
                        'relative h-6 w-11 rounded-full transition-colors',
                        darkMode ? 'bg-primary-600' : 'bg-surface-300'
                      )}
                    >
                      <span className={cn(
                        'absolute top-0.5 h-5 w-5 rounded-full bg-white transition-transform',
                        darkMode ? 'left-5.5' : 'left-0.5'
                      )} />
                    </button>
                  </div>
                </CardContent>
              </Card>
            </div>
          )}

          {activeSection === 'memory' && (
            <div className="max-w-2xl space-y-6">
              <h2 className="text-xl font-bold text-surface-900 dark:text-white">Memory Settings</h2>
              <Card>
                <CardContent className="space-y-4 p-5">
                  {[
                    { label: 'Enable Long-Term Memory', key: 'enableLongTermMemory' },
                    { label: 'Remember Project Preferences', key: 'rememberProjectPreferences' },
                    { label: 'Remember Workflow History', key: 'rememberWorkflowHistory' },
                  ].map(item => (
                    <div key={item.key} className="flex items-center justify-between">
                      <span className="text-sm text-surface-700 dark:text-surface-300">{item.label}</span>
                      <input
                        type="checkbox"
                        checked={settings[item.key as keyof typeof settings] as boolean}
                        onChange={(e) => updateSettings({ [item.key]: e.target.checked })}
                        className="h-4 w-4 rounded border-surface-300 text-primary-600"
                      />
                    </div>
                  ))}
                  <Button variant="danger" size="sm" className="mt-2">
                    Clear All Memories
                  </Button>
                </CardContent>
              </Card>
            </div>
          )}

          {['ai', 'privacy', 'notifications', 'billing', 'api'].includes(activeSection) && (
            <div className="max-w-2xl">
              <h2 className="text-xl font-bold text-surface-900 dark:text-white capitalize">{activeSection}</h2>
              <p className="mt-2 text-surface-500">This section is coming soon.</p>
            </div>
          )}
        </motion.div>
      </div>
    </div>
  )
}

function cn(...classes: (string | undefined | false)[]) {
  return classes.filter(Boolean).join(' ')
}