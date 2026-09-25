import { useState } from 'react'
import { FileText, Database, RefreshCw, Trash2, Upload } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { EmptyState } from '@/components/ui/EmptyState'
import { useRuntimeSnapshot } from '@/hooks/useRuntimeSnapshot'
import { deleteKnowledgeDocument, reindexKnowledgeDocument } from '@/services/apiClient'

export function KnowledgePage() {
  const { snapshot, error } = useRuntimeSnapshot()
  const [documentAction, setDocumentAction] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
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
      {actionError && <EmptyState title="Document action failed" description={actionError} icon={<Database className="h-5 w-5" />} />}

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
                <div className="knowledge-document-actions">
                  <StatusBadge status={document.status === 'indexed' ? 'completed' : document.status} />
                  <button
                    title="Re-index document"
                    disabled={documentAction === document.id}
                    onClick={async () => {
                      setDocumentAction(document.id)
                      setActionError(null)
                      try { await reindexKnowledgeDocument(document.id) }
                      catch (caught) { setActionError(caught instanceof Error ? caught.message : 'Re-index failed.') }
                      finally { setDocumentAction(null) }
                    }}
                  >
                    <RefreshCw className={`h-4 w-4 ${documentAction === document.id ? 'spin-icon' : ''}`} />
                  </button>
                  <button
                    title="Delete document"
                    disabled={documentAction === document.id}
                    onClick={async () => {
                      if (!window.confirm(`Delete ${document.name} and all indexed chunks?`)) return
                      setDocumentAction(document.id)
                      setActionError(null)
                      try { await deleteKnowledgeDocument(document.id) }
                      catch (caught) { setActionError(caught instanceof Error ? caught.message : 'Delete failed.') }
                      finally { setDocumentAction(null) }
                    }}
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
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
