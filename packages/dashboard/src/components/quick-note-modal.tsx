import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { useUIStore } from '@/stores/ui-store'
import { useVaultStore } from '@/stores/vault-store'
import { encodeBase64 } from '@/lib/utils'
import { useIngestNote } from '@/api/hooks/use-notes'
import { toast } from 'sonner'
import { Eye, EyeOff, X } from 'lucide-react'

export function QuickNoteModal() {
  const isOpen = useUIStore((s) => s.isQuickNoteOpen)
  const toggleQuickNote = useUIStore((s) => s.toggleQuickNote)
  const writerVaultId = useVaultStore((s) => s.writerVaultId)
  const writerVaultName = useVaultStore((s) => s.writerVaultName)
  const ingestNote = useIngestNote()

  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [tagInput, setTagInput] = useState('')
  const [tags, setTags] = useState<string[]>(['dashboard', 'quick-note'])
  const [showPreview, setShowPreview] = useState(false)

  function addTag() {
    const tag = tagInput.trim().toLowerCase()
    if (tag && !tags.includes(tag)) {
      setTags([...tags, tag])
    }
    setTagInput('')
  }

  function removeTag(tag: string) {
    setTags(tags.filter((t) => t !== tag))
  }

  function handleOpenChange(open: boolean) {
    if (!open) {
      setTitle('')
      setContent('')
      setTagInput('')
      setTags(['dashboard', 'quick-note'])
      setShowPreview(false)
      toggleQuickNote()
    }
  }

  async function handleSave() {
    if (!content.trim()) return

    try {
      await ingestNote.mutateAsync({
        name: title.trim() || 'Quick Note',
        description: 'Note captured from dashboard',
        content: encodeBase64(content),
        tags,
        vault_id: writerVaultId || undefined,
      })
      handleOpenChange(false)
      toast.success('Note saved successfully!')
    } catch (err) {
      toast.error(`Failed to save note: ${err instanceof Error ? err.message : String(err)}`)
    }
  }

  return (
    <Dialog open={isOpen} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Quick Note</DialogTitle>
          <DialogDescription>
            Capture a quick thought. Writing vault: {writerVaultName || 'Default'}
          </DialogDescription>
        </DialogHeader>

        {/* Title */}
        <Input
          placeholder="Note title (optional)"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="text-sm"
        />

        {/* Content / Preview toggle */}
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground">
            {showPreview ? 'Preview' : 'Markdown'}
          </span>
          <button
            type="button"
            onClick={() => setShowPreview(!showPreview)}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            {showPreview ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
            {showPreview ? 'Edit' : 'Preview'}
          </button>
        </div>

        {showPreview ? (
          <div className="min-h-[120px] rounded-md border border-border bg-muted/30 p-3 prose prose-invert prose-sm max-w-none">
            <ReactMarkdown>{content || '*No content yet*'}</ReactMarkdown>
          </div>
        ) : (
          <Textarea
            placeholder="Type your note here... (supports Markdown)"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            className="min-h-[120px] resize-none"
            autoFocus
          />
        )}

        {/* Tags */}
        <div>
          <div className="flex flex-wrap gap-1 mb-2">
            {tags.map((tag) => (
              <Badge key={tag} variant="secondary" className="text-xs gap-1">
                {tag}
                <button type="button" onClick={() => removeTag(tag)} className="hover:text-destructive">
                  <X className="h-2.5 w-2.5" />
                </button>
              </Badge>
            ))}
          </div>
          <Input
            placeholder="Add tag and press Enter"
            value={tagInput}
            onChange={(e) => setTagInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                addTag()
              }
            }}
            className="h-8 text-xs"
          />
        </div>

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
