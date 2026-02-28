import { useState } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard,
  Share2,
  GitBranch,
  Search,
  FileSearch,
  Activity,
  Settings,
  CircleHelp,
  Menu,
  Keyboard,
  Database,
  Sun,
  Moon,
  RefreshCw,
  Clock,
  Workflow,
} from 'lucide-react'
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useUIStore } from '@/stores/ui-store'
import { useVaultStore } from '@/stores/vault-store'
import { useReflectionQueue } from '@/api/hooks/use-reflections'
import { cn } from '@/lib/utils'

const navItems = [
  { to: '/', icon: LayoutDashboard, label: 'Overview' },
  { to: '/entity', icon: Share2, label: 'Entity Graph' },
  { to: '/lineage', icon: GitBranch, label: 'Lineage' },
  { to: '/search', icon: Search, label: 'Memory Search' },
  { to: '/doc-search', icon: FileSearch, label: 'Note Search' },
  { to: '/status', icon: Activity, label: 'System Status' },
  { to: '/reflection', icon: RefreshCw, label: 'Reflections' },
  { to: '/timeline', icon: Clock, label: 'Timeline' },
  { to: '/knowledge-flow', icon: Workflow, label: 'Knowledge Flow' },
]

const bottomItems = [
  { to: '/settings', icon: Settings, label: 'Settings' },
  { to: '#', icon: CircleHelp, label: 'Help' },
]

function NavItem({
  to,
  icon: Icon,
  label,
  collapsed,
  badge,
}: {
  to: string
  icon: React.ComponentType<{ className?: string }>
  label: string
  collapsed?: boolean
  badge?: React.ReactNode
}) {
  const link = (
    <NavLink
      to={to}
      end={to === '/'}
      aria-label={label}
      className={({ isActive }) =>
        cn(
          'flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-all duration-200',
          isActive
            ? 'bg-[rgba(59,130,246,0.1)] text-primary'
            : 'text-muted-foreground hover:bg-hover hover:text-foreground',
          collapsed && 'justify-center px-2',
        )
      }
    >
      <Icon className="h-4 w-4 shrink-0" />
      {!collapsed && <span>{label}</span>}
      {!collapsed && badge}
    </NavLink>
  )

  if (collapsed) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>{link}</TooltipTrigger>
        <TooltipContent side="right">{label}</TooltipContent>
      </Tooltip>
    )
  }

  return link
}

function ReflectionBadge() {
  const { data: queue } = useReflectionQueue()
  const count = queue?.length ?? 0
  if (count === 0) return null
  return (
    <span className="ml-auto rounded-full bg-primary/20 px-1.5 py-0.5 text-[10px] font-medium text-primary">
      {count}
    </span>
  )
}

function SidebarContent({ collapsed }: { collapsed: boolean }) {
  const navigate = useNavigate()
  const { writerVaultName } = useVaultStore()

  return (
    <>
      <div className={cn('flex items-center gap-2 px-4 py-5', collapsed && 'justify-center px-2')}>
        <img src="/logo.png" alt="Memex" className="h-8 w-8 shrink-0 dark-invert" />
        {!collapsed && <span className="text-lg font-semibold text-foreground">Memex</span>}
      </div>

      <nav className="flex flex-1 flex-col gap-1 px-3" aria-label="Main navigation">
        {navItems.map((item) => (
          <NavItem
            key={item.to}
            {...item}
            collapsed={collapsed}
            badge={item.to === '/status' ? <ReflectionBadge /> : undefined}
          />
        ))}
      </nav>

      <nav className="flex flex-col gap-1 px-3 pb-4" aria-label="Secondary navigation">
        {bottomItems.map((item) => (
          <NavItem key={item.label} {...item} collapsed={collapsed} />
        ))}
        <div className={cn('mt-2 border-t border-border pt-2', collapsed && 'px-1')}>
          <ThemeToggle collapsed={collapsed} />
          {!collapsed && (
            <>
              <button
                onClick={() => navigate('/settings')}
                className="flex items-center gap-2 px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground hover:bg-hover rounded-md transition-colors w-full"
              >
                <Database className="h-3 w-3" />
                <span className="truncate">{writerVaultName || 'No vault'}</span>
              </button>
              <div className="flex items-center gap-2 px-3 py-1 text-xs text-muted-foreground">
                <Keyboard className="h-3 w-3" aria-hidden="true" />
                <span>Ctrl+K to search</span>
              </div>
            </>
          )}
        </div>
      </nav>
    </>
  )
}

function ThemeToggle({ collapsed }: { collapsed: boolean }) {
  const [isDark, setIsDark] = useState(() => !document.documentElement.classList.contains('light'));

  function toggle() {
    const next = !isDark;
    setIsDark(next);
    if (next) {
      document.documentElement.classList.remove('light');
      document.documentElement.style.backgroundColor = '#0D0D0D';
      document.documentElement.style.color = '#EDEDED';
      localStorage.setItem('memex_theme', 'dark');
    } else {
      document.documentElement.classList.add('light');
      document.documentElement.style.backgroundColor = '#FFFFFF';
      document.documentElement.style.color = '#171717';
      localStorage.setItem('memex_theme', 'light');
    }
  }

  return (
    <button
      onClick={toggle}
      className={cn(
        'flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted-foreground hover:bg-hover hover:text-foreground transition-colors',
        collapsed && 'justify-center px-2',
      )}
      aria-label="Toggle theme"
    >
      {isDark ? <Sun className="h-4 w-4 shrink-0" /> : <Moon className="h-4 w-4 shrink-0" />}
      {!collapsed && <span>{isDark ? 'Light Mode' : 'Dark Mode'}</span>}
    </button>
  );
}

export function Sidebar() {
  const { isSidebarCollapsed, toggleSidebar } = useUIStore()

  return (
    <>
      {/* Desktop sidebar */}
      <aside
        className={cn(
          'hidden lg:flex h-screen flex-col border-r border-border bg-sidebar transition-[width] duration-200',
          isSidebarCollapsed ? 'w-16' : 'w-60',
        )}
      >
        <SidebarContent collapsed={isSidebarCollapsed} />
        <button
          onClick={toggleSidebar}
          className="mx-3 mb-3 rounded-lg p-2 text-muted-foreground hover:bg-hover hover:text-foreground transition-colors"
          aria-label={isSidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          <Menu className="h-4 w-4 mx-auto" />
        </button>
      </aside>

      {/* Mobile sidebar (Sheet) */}
      <div className="lg:hidden fixed top-0 left-0 z-40 p-2">
        <Sheet>
          <SheetTrigger asChild>
            <Button variant="ghost" size="icon" aria-label="Open navigation menu">
              <Menu className="h-5 w-5" />
            </Button>
          </SheetTrigger>
          <SheetContent side="left" className="w-60 bg-sidebar p-0">
            <SheetTitle className="sr-only">Navigation Menu</SheetTitle>
            <SidebarContent collapsed={false} />
          </SheetContent>
        </Sheet>
      </div>
    </>
  )
}
