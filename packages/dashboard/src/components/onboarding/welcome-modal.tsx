import { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Brain, Search, Share2, FileText } from 'lucide-react';

const ONBOARDING_KEY = 'memex_onboarding_completed';

export function WelcomeModal() {
  const completed = localStorage.getItem(ONBOARDING_KEY);
  const [isOpen, setIsOpen] = useState(!completed);

  function handleComplete() {
    localStorage.setItem(ONBOARDING_KEY, 'true');
    setIsOpen(false);
  }

  return (
    <Dialog open={isOpen} onOpenChange={(open) => { if (!open) handleComplete(); }}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-xl">Welcome to Memex</DialogTitle>
          <DialogDescription>
            Your personal knowledge management system powered by AI
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-2 gap-3 py-4">
          <div className="rounded-lg border border-border p-4 text-center">
            <Brain className="mx-auto mb-2 h-8 w-8 text-primary" />
            <h4 className="text-sm font-semibold text-foreground">Memory Search</h4>
            <p className="mt-1 text-xs text-muted-foreground">Search across all extracted memories and facts</p>
          </div>
          <div className="rounded-lg border border-border p-4 text-center">
            <FileText className="mx-auto mb-2 h-8 w-8 text-emerald-500" />
            <h4 className="text-sm font-semibold text-foreground">Note Search</h4>
            <p className="mt-1 text-xs text-muted-foreground">Find specific passages in your source documents</p>
          </div>
          <div className="rounded-lg border border-border p-4 text-center">
            <Share2 className="mx-auto mb-2 h-8 w-8 text-purple-500" />
            <h4 className="text-sm font-semibold text-foreground">Entity Graph</h4>
            <p className="mt-1 text-xs text-muted-foreground">Explore connections between people, places, and concepts</p>
          </div>
          <div className="rounded-lg border border-border p-4 text-center">
            <Search className="mx-auto mb-2 h-8 w-8 text-amber-500" />
            <h4 className="text-sm font-semibold text-foreground">Quick Search</h4>
            <p className="mt-1 text-xs text-muted-foreground">Press Ctrl+K anytime to search across everything</p>
          </div>
        </div>

        <div className="flex justify-end">
          <Button onClick={handleComplete}>Get Started</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
