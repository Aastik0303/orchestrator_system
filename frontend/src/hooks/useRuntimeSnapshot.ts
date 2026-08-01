import { useEffect, useState } from 'react'
import { getRuntimeSnapshot, type RuntimeSnapshot } from '@/services/apiClient'

export function useRuntimeSnapshot(intervalMs = 2000) {
  const [snapshot, setSnapshot] = useState<RuntimeSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let mounted = true
    let timer: number | undefined

    async function load() {
      try {
        const nextSnapshot = await getRuntimeSnapshot()
        if (!mounted) return
        setSnapshot(nextSnapshot)
        setError(null)
      } catch (caught) {
        if (!mounted) return
        setError(caught instanceof Error ? caught.message : 'Could not load backend runtime data.')
      } finally {
        if (mounted) setLoading(false)
      }
    }

    load()
    timer = window.setInterval(load, intervalMs)

    return () => {
      mounted = false
      if (timer) window.clearInterval(timer)
    }
  }, [intervalMs])

  return { snapshot, loading, error }
}
