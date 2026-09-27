import { useState } from 'react'
import { Download } from 'lucide-react'
import { downloadArtifact } from '@/services/apiClient'

// Output files produced by the backend data analyst (app/agents/data_transform.py)
// and embedded in responses as fenced ```download JSON blocks.
export type DownloadSpec = { name: string; path: string; rows?: number; columns?: number; format?: string }

export function parseDownloadSpec(source: string): DownloadSpec | null {
  try {
    const spec = JSON.parse(source)
    return spec && typeof spec.name === 'string' && typeof spec.path === 'string' && /^[0-9a-f]{16}\/[^/]+$/.test(spec.path)
      ? spec as DownloadSpec
      : null
  } catch {
    return null
  }
}

export function DownloadCard({ spec }: { spec: DownloadSpec }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const details = [
    spec.format?.toUpperCase(),
    typeof spec.rows === 'number' ? `${spec.rows.toLocaleString()} rows` : null,
    typeof spec.columns === 'number' ? `${spec.columns} columns` : null,
  ].filter(Boolean).join(' · ')

  const download = async () => {
    setBusy(true)
    setError(null)
    try {
      await downloadArtifact(spec.path, spec.name)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Download failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="download-card">
      <div>
        <strong title={spec.name}>{spec.name}</strong>
        {error ? <span className="download-card-error">{error}</span> : details && <span>{details}</span>}
      </div>
      <button type="button" onClick={download} disabled={busy}>
        <Download size={14} aria-hidden="true" />
        {busy ? 'Downloading…' : 'Download'}
      </button>
    </div>
  )
}
