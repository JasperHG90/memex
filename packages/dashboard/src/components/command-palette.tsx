import { useState, useEffect } from 'react'
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
  Brain,
  FileText,
  Loader2,
  RefreshCw,
  Clock,
  Workflow,
} from 'lucide-react'
import { useUIStore } from '@/stores/ui-store'
import { useMemorySearch } from '@/api/hooks/use-memories'
import { useNoteSearch } from '@/api/hooks/use-notes'
import type { MemoryUnitDTO, NoteSearchResult } from '@/api/generated'

const pages = [
  { to: '/', icon: LayoutDashboard, label: 'Overview' },
  { to: '/entity', icon: Share2, label: 'Entity Graph' },
  { to: '/lineage', icon: GitBranch, label: 'Lineage' },
  { to: '/search', icon: Search, label: 'Memory Search' },
  { to: '/doc-search', icon: FileSearch, label: 'Note Search' },
  { to: '/status', icon: Activity, label: 'System Status' },
  { to: '/settings', icon: Settings, label: 'Settings' },
  { to: '/reflection', icon: RefreshCw, label: 'Reflections' },
  { to: '/timeline', icon: Clock, label: 'Timeline' },
  { to: '/knowledge-flow', icon: Workflow, label: 'Knowledge Flow' },
]

export function CommandPalette() {
  const isOpen = useUIStore((s) => s.isCommandPaletteOpen)
  const setOpen = useUIStore((s) => s.setCommandPaletteOpen)
  const toggleQuickNote = useUIStore((s) => s.toggleQuickNote)
  const toggleFullscreen = useUIStore((s) => s.toggleFullscreen)
  const navigate = useNavigate()

  const [searchQuery, setSearchQuery] = useState('')
  const memorySearch = useMemorySearch()
  const noteSearch = useNoteSearch()

  // Debounced search effect
  const memoryMutate = memorySearch.mutate
  const noteMutate = noteSearch.mutate
  useEffect(() => {
    if (!searchQuery.trim() || searchQuery.length < 3) return
    const timer = setTimeout(() => {
      memoryMutate({
        query: searchQuery,
        limit: 5,
        offset: 0,
        rerank: true,
        include_vectors: false,
        include_stale: false,
        skip_opinion_formation: true,
      })
      noteMutate({
        query: searchQuery,
        limit: 5,
        strategies: ['semantic', 'keyword'],
        expand_query: false,
        fusion_strategy: 'rrf',
        reason: false,
        summarize: false,
      })
    }, 300)
    return () => clearTimeout(timer)
  }, [searchQuery, memoryMutate, noteMutate])

  function handleOpenChange(open: boolean) {
    setOpen(open)
    if (!open) {
      setSearchQuery('')
      memorySearch.reset()
      noteSearch.reset()
    }
  }

  function handleSelect(callback: () => void) {
    setOpen(false)
    callback()
  }

  return (
    <CommandDialog open={isOpen} onOpenChange={handleOpenChange}>
      <CommandInput
        placeholder="Search pages, actions, or knowledge..."
        value={searchQuery}
        onValueChange={setSearchQuery}
      />
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

        {/* Memory search results */}
        {(memorySearch.data as MemoryUnitDTO[] | undefined)?.length ? (
          <>
            <CommandSeparator />
            <CommandGroup heading="Memories">
              {(memorySearch.data as MemoryUnitDTO[]).slice(0, 5).map((mem) => (
                <CommandItem
                  key={mem.id}
                  onSelect={() => handleSelect(() => navigate(`/search?q=${encodeURIComponent(searchQuery)}`))}
                >
                  <Brain className="h-4 w-4" />
                  <span className="truncate">{mem.text.slice(0, 80)}{mem.text.length > 80 ? '...' : ''}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          </>
        ) : null}

        {/* Note search results */}
        {(noteSearch.data as NoteSearchResult[] | undefined)?.length ? (
          <>
            <CommandSeparator />
            <CommandGroup heading="Notes">
              {(noteSearch.data as NoteSearchResult[]).slice(0, 5).map((note) => (
                <CommandItem
                  key={note.note_id}
                  onSelect={() => handleSelect(() => navigate(`/doc-search?q=${encodeURIComponent(searchQuery)}`))}
                >
                  <FileText className="h-4 w-4" />
                  <span className="truncate">
                    {String((note.metadata as Record<string, unknown>)?.title ?? 'Untitled')}
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          </>
        ) : null}

        {/* Loading indicator */}
        {(memorySearch.isPending || noteSearch.isPending) && searchQuery.length >= 3 && (
          <div className="flex items-center justify-center py-4">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
            <span className="ml-2 text-xs text-muted-foreground">Searching...</span>
          </div>
        )}
      </CommandList>
    </CommandDialog>
  )
}
