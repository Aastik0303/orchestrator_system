import { FileText, Database, Upload } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { EmptyState } from '@/components/ui/EmptyState'
import { useRuntimeSnapshot } from '@/hooks/useRuntimeSnapshot'

export function KnowledgePage() {
  const { snapshot, error } = useRuntimeSnapshot()
  const documents = snapshot?.documents ?? []
  const totalChunks = documents.reduce((total, document) => total + document.chunk_count, 0)

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-surface-900 dark:text-white">Knowledge</h2>
          <p className="mt-1 text-sm text-surface-500 dark:text-surface-400">Documents stored by the running backend.</p>
        </div>
      </div>

      {error && <EmptyState title="Could not load backend knowledge" description={error} icon={<Database className="h-5 w-5" />} />}

      <div className="grid gap-4 sm:grid-cols-2">
        <Card>
          <CardContent className="p-5">
            <p className="text-xs text-surface-500">Documents</p>
            <p className="text-2xl font-bold text-surface-900 dark:text-white">{documents.length}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5">
            <p className="text-xs text-surface-500">Chunks</p>
            <p className="text-2xl font-bold text-surface-900 dark:text-white">{totalChunks}</p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Uploaded Documents</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {documents.length === 0 ? (
            <EmptyState title="No backend documents yet" description="Use the + button in Workspace to upload documents into backend Knowledge." icon={<Upload className="h-5 w-5" />} />
          ) : (
            documents.map(document => (
              <div key={document.id} className="knowledge-document-row">
                <div className="flex items-center gap-3">
                  <div className="knowledge-document-icon">
                    <FileText className="h-4 w-4" />
                  </div>
                  <div>
                    <p className="font-medium text-surface-900 dark:text-white">{document.name}</p>
                    <p className="text-xs text-surface-500">{formatBytes(document.size)} - {document.chunk_count} chunks - {document.content_type || 'unknown type'}</p>
                  </div>
                </div>
                <StatusBadge status={document.status === 'indexed' ? 'completed' : document.status} />
              </div>
            ))
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
