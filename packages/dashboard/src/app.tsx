import { Outlet } from 'react-router-dom'
import { Sidebar } from '@/components/layout/sidebar'
import { ConnectionBanner } from '@/components/shared/connection-banner'
import { QuickNoteModal } from '@/components/quick-note-modal'
import { CommandPalette } from '@/components/command-palette'
import { useKeyboardShortcuts } from '@/hooks/use-keyboard-shortcuts'
import { useConnectionStatus } from '@/hooks/use-connection-status'
import { useUIStore } from '@/stores/ui-store'

export default function App() {
  const { isConnected } = useConnectionStatus()
  const { toggleCommandPalette, toggleQuickNote, setCommandPaletteOpen } = useUIStore()

  useKeyboardShortcuts({
    onCommandPalette: toggleCommandPalette,
    onQuickNote: toggleQuickNote,
    onEscape: () => setCommandPaletteOpen(false),
  })

  return (
    <div className="flex h-screen bg-background text-foreground">
      <ConnectionBanner isError={!isConnected} />
      <Sidebar />
      <main className="flex-1 overflow-auto p-4 md:p-6 pt-14 lg:pt-6">
        <Outlet />
      </main>
      <QuickNoteModal />
      <CommandPalette />
    </div>
  )
}
