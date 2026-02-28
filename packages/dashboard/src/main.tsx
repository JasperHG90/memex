import { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Toaster } from '@/components/ui/sonner'
import { PageSkeleton } from '@/components/shared/page-skeleton'
import App from './app'
import './index.css'

// Restore saved theme before render to avoid flash
const savedTheme = localStorage.getItem('memex_theme');
if (savedTheme === 'light') {
  document.documentElement.classList.add('light');
  document.documentElement.style.backgroundColor = '#FFFFFF';
  document.documentElement.style.color = '#171717';
}

const Overview = lazy(() => import('@/pages/overview'))
const EntityGraph = lazy(() => import('@/pages/entity-graph'))
const Lineage = lazy(() => import('@/pages/lineage'))
const MemorySearch = lazy(() => import('@/pages/memory-search'))
const NoteSearch = lazy(() => import('@/pages/note-search'))
const SystemStatus = lazy(() => import('@/pages/system-status'))
const Settings = lazy(() => import('@/pages/settings'))
const Reflection = lazy(() => import('@/pages/reflection'))
const MemoryTimeline = lazy(() => import('@/pages/timeline'))
const KnowledgeFlow = lazy(() => import('@/pages/knowledge-flow'))

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
    },
  },
})

function SuspenseWrapper({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<PageSkeleton />}>{children}</Suspense>
}

const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <SuspenseWrapper><Overview /></SuspenseWrapper> },
      { path: 'entity', element: <SuspenseWrapper><EntityGraph /></SuspenseWrapper> },
      { path: 'lineage', element: <SuspenseWrapper><Lineage /></SuspenseWrapper> },
      { path: 'search', element: <SuspenseWrapper><MemorySearch /></SuspenseWrapper> },
      { path: 'doc-search', element: <SuspenseWrapper><NoteSearch /></SuspenseWrapper> },
      { path: 'status', element: <SuspenseWrapper><SystemStatus /></SuspenseWrapper> },
      { path: 'settings', element: <SuspenseWrapper><Settings /></SuspenseWrapper> },
      { path: 'reflection', element: <SuspenseWrapper><Reflection /></SuspenseWrapper> },
      { path: 'timeline', element: <SuspenseWrapper><MemoryTimeline /></SuspenseWrapper> },
      { path: 'knowledge-flow', element: <SuspenseWrapper><KnowledgeFlow /></SuspenseWrapper> },
    ],
  },
])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <RouterProvider router={router} />
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  </StrictMode>,
)
