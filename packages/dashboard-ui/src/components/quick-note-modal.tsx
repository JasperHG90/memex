import { useState } from 'react'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { useUIStore } from '@/stores/ui-store'
import { useVaultStore } from '@/stores/vault-store'
import { useIngestNote } from '@/api/hooks/use-notes'
import { toast } from 'sonner'

export function QuickNoteModal() {
  const isOpen = useUIStore((s) => s.isQuickNoteOpen)
  const toggleQuickNote = useUIStore((s) => s.toggleQuickNote)
  const writerVaultId = useVaultStore((s) => s.writerVaultId)
  const ingestNote = useIngestNote()
  const [content, setContent] = useState('')

  function handleOpenChange(open: boolean) {
    if (!open) {
      setContent('')
      toggleQuickNote()
    }
  }

  async function handleSave() {
    if (!content.trim()) return

    try {
      await ingestNote.mutateAsync({
        name: 'Quick Note',
        description: 'Note captured from dashboard',
        content: btoa(content),
        tags: ['dashboard', 'quick-note'],
        vault_id: writerVaultId || undefined,
      })
      setContent('')
      toggleQuickNote()
      toast.success('Note saved successfully!')
    } catch (err) {
      toast.error(`Failed to save note: ${err instanceof Error ? err.message : String(err)}`)
    }
  }

  return (
    <Dialog open={isOpen} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Quick Note</DialogTitle>
          <DialogDescription>
            Capture a quick thought or note. It will be ingested into your writer vault.
          </DialogDescription>
        </DialogHeader>
        <Textarea
          placeholder="Type your note here..."
          value={content}
          onChange={(e) => setContent(e.target.value)}
          className="min-h-[120px] resize-none"
          autoFocus
        />
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => handleOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={handleSave}
            disabled={!content.trim() || ingestNote.isPending}
          >
            {ingestNote.isPending ? 'Saving...' : 'Save'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
