import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  CommandDialog,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandSeparator,
} from '@/components/ui/command'
import {
  LayoutDashboard,
  Share2,
  GitBranch,
  Search,
  FileSearch,
  Activity,
  Settings,
  StickyNote,
  Maximize,
} from 'lucide-react'
import { useUIStore } from '@/stores/ui-store'

const pages = [
  { to: '/', icon: LayoutDashboard, label: 'Overview' },
  { to: '/entity', icon: Share2, label: 'Entity Graph' },
  { to: '/lineage', icon: GitBranch, label: 'Lineage' },
  { to: '/search', icon: Search, label: 'Memory Search' },
  { to: '/doc-search', icon: FileSearch, label: 'Note Search' },
  { to: '/status', icon: Activity, label: 'System Status' },
  { to: '/settings', icon: Settings, label: 'Settings' },
]

export function CommandPalette() {
  const isOpen = useUIStore((s) => s.isCommandPaletteOpen)
  const setOpen = useUIStore((s) => s.setCommandPaletteOpen)
  const toggleQuickNote = useUIStore((s) => s.toggleQuickNote)
  const toggleFullscreen = useUIStore((s) => s.toggleFullscreen)
  const navigate = useNavigate()

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault()
        setOpen(!isOpen)
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [isOpen, setOpen])

  function handleSelect(callback: () => void) {
    setOpen(false)
    callback()
  }

  return (
    <CommandDialog open={isOpen} onOpenChange={setOpen}>
      <CommandInput placeholder="Search pages or actions..." />
      <CommandList>
        <CommandEmpty>No results found.</CommandEmpty>
        <CommandGroup heading="Pages">
          {pages.map(({ to, icon: Icon, label }) => (
            <CommandItem
              key={to}
              onSelect={() => handleSelect(() => navigate(to))}
            >
              <Icon className="h-4 w-4" />
              <span>{label}</span>
            </CommandItem>
          ))}
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading="Actions">
          <CommandItem
            onSelect={() =>
              handleSelect(() => toggleQuickNote())
            }
          >
            <StickyNote className="h-4 w-4" />
            <span>Quick Note</span>
          </CommandItem>
          <CommandItem
            onSelect={() =>
              handleSelect(() => toggleFullscreen())
            }
          >
            <Maximize className="h-4 w-4" />
            <span>Toggle Fullscreen</span>
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  )
}
