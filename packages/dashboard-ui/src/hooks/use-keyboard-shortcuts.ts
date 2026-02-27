import { useEffect } from 'react';

interface KeyboardShortcutHandlers {
  onCommandPalette?: () => void;
  onQuickNote?: () => void;
  onEscape?: () => void;
}

export function useKeyboardShortcuts({
  onCommandPalette,
  onQuickNote,
  onEscape,
}: KeyboardShortcutHandlers) {
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      // Skip if user is typing in an input/textarea
      const target = e.target as HTMLElement;
      if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable) {
        // Allow Escape even in inputs
        if (e.key !== 'Escape') return;
      }

      // Cmd/Ctrl + K: Command palette
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        onCommandPalette?.();
      }

      // Cmd/Ctrl + N: Quick note
      if ((e.metaKey || e.ctrlKey) && e.key === 'n') {
        e.preventDefault();
        onQuickNote?.();
      }

      // Escape: Close modals
      if (e.key === 'Escape') {
        onEscape?.();
      }
    }

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onCommandPalette, onQuickNote, onEscape]);
}
