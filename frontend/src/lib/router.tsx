import React, { createContext, useContext, useEffect, useMemo, useState } from 'react'

type NavigateOptions = { replace?: boolean }
type RouterContextValue = {
  path: string
  navigate: (to: string, options?: NavigateOptions) => void
}

const RouterContext = createContext<RouterContextValue | null>(null)

function normalizePath(path: string) {
  return path.split('?')[0].replace(/\/+$/, '') || '/'
}

function useRouter() {
  const context = useContext(RouterContext)
  if (!context) {
    throw new Error('Router hooks must be used inside BrowserRouter')
  }
  return context
}

export function BrowserRouter({ children }: { children: React.ReactNode }) {
  const [path, setPath] = useState(() => normalizePath(window.location.pathname))

  useEffect(() => {
    const onPopState = () => setPath(normalizePath(window.location.pathname))
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const value = useMemo<RouterContextValue>(() => ({
    path,
    navigate(to, options) {
      const nextPath = normalizePath(to)
      if (nextPath === path) return
      if (options?.replace) {
        window.history.replaceState(null, '', nextPath)
      } else {
        window.history.pushState(null, '', nextPath)
      }
      setPath(nextPath)
    },
  }), [path])

  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>
}

export function useNavigate() {
  return useRouter().navigate
}

export function useLocation() {
  const { path } = useRouter()
  return { pathname: path }
}

export function useParams() {
  const { path } = useRouter()
  const routePattern = sessionStorage.getItem('active-route-pattern') || ''
  return matchRoute(routePattern, path).params
}

export function Navigate({ to, replace }: { to: string; replace?: boolean }) {
  const navigate = useNavigate()
  useEffect(() => {
    navigate(to, { replace })
  }, [navigate, replace, to])
  return null
}

export function Route(_props: { path: string; element: React.ReactElement }) {
  return null
}

export function Routes({ children }: { children: React.ReactNode }) {
  const { path } = useRouter()
  const routes = React.Children.toArray(children).filter(React.isValidElement)

  for (const route of routes) {
    const props = route.props as { path?: string; element?: React.ReactElement }
    if (!props.path || !props.element) continue
    const match = matchRoute(props.path, path)
    if (match.matched) {
      sessionStorage.setItem('active-route-pattern', props.path)
      return props.element
    }
  }

  return null
}

function matchRoute(pattern: string, path: string) {
  if (pattern === '*') return { matched: true, params: {} as Record<string, string> }
  const patternParts = normalizePath(pattern).split('/').filter(Boolean)
  const pathParts = normalizePath(path).split('/').filter(Boolean)
  const params: Record<string, string> = {}

  if (patternParts.length !== pathParts.length) {
    return { matched: false, params }
  }

  for (let index = 0; index < patternParts.length; index += 1) {
    const patternPart = patternParts[index]
    const pathPart = pathParts[index]
    if (patternPart.startsWith(':')) {
      params[patternPart.slice(1)] = decodeURIComponent(pathPart)
    } else if (patternPart !== pathPart) {
      return { matched: false, params: {} }
    }
  }

  return { matched: true, params }
}
