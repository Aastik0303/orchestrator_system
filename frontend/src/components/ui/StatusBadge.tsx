import { Badge } from '@/components/ui/Badge'

const labels: Record<string, string> = {
  completed: 'Completed',
  success: 'Success',
  running: 'Running',
  queued: 'Queued',
  retrying: 'Retrying',
  pending: 'Pending',
  failed: 'Failed',
  timeout: 'Timeout',
  blocked: 'Blocked',
  cancelled: 'Cancelled',
  approval_required: 'Approval',
}

const variants: Record<string, 'success' | 'danger' | 'warning' | 'neutral'> = {
  completed: 'success',
  success: 'success',
  running: 'warning',
  queued: 'warning',
  retrying: 'warning',
  failed: 'danger',
  timeout: 'danger',
  blocked: 'danger',
}

export function StatusBadge({ status }: { status: string }) {
  const key = status.toLowerCase()
  return <Badge variant={variants[key] ?? 'neutral'}>{labels[key] || status}</Badge>
}
