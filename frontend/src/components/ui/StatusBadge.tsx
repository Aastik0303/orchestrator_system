import { Badge } from '@/components/ui/Badge'

const labels: Record<string, string> = {
  completed: 'Completed',
  running: 'Running',
  failed: 'Failed',
  pending: 'Pending',
  approval_required: 'Approval',
}

export function StatusBadge({ status }: { status: string }) {
  const variant = status === 'completed'
    ? 'success'
    : status === 'failed'
      ? 'danger'
      : status === 'running'
        ? 'warning'
        : 'neutral'

  return <Badge variant={variant}>{labels[status] || status}</Badge>
}
