import type { ReactNode } from 'react'
import { Database } from 'lucide-react'

export function EmptyState({ title, description, icon }: { title: string; description: string; icon?: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="empty-state-icon">{icon || <Database className="h-5 w-5" />}</div>
      <div>
        <p className="font-medium text-surface-900 dark:text-white">{title}</p>
        <p className="text-sm text-surface-500">{description}</p>
      </div>
    </div>
  )
}
