import type { ReactNode } from 'react'

export class QueryClient {
  constructor(public readonly options: unknown = {}) {}
}

export function QueryClientProvider({ children }: { client: QueryClient; children: ReactNode }) {
  return <>{children}</>
}
