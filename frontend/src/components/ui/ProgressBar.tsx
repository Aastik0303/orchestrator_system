export function ProgressBar({ value, max = 100, size = 'md' }: { value: number; max?: number; size?: 'sm' | 'md' }) {
  const percent = Math.min(100, Math.max(0, (value / max) * 100))
  return (
    <div className={`ui-progress ui-progress-${size}`}>
      <div className="ui-progress-fill" style={{ width: `${percent}%` }} />
    </div>
  )
}
