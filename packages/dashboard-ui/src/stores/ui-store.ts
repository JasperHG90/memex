import { create } from 'zustand';

interface UIStore {
  isFullscreen: boolean;
  isQuickNoteOpen: boolean;
  isSidebarCollapsed: boolean;
  isCommandPaletteOpen: boolean;

  toggleFullscreen: () => void;
  toggleQuickNote: () => void;
  toggleSidebar: () => void;
  toggleCommandPalette: () => void;
  setCommandPaletteOpen: (open: boolean) => void;
}

export const useUIStore = create<UIStore>((set) => ({
  isFullscreen: false,
  isQuickNoteOpen: false,
  isSidebarCollapsed: false,
  isCommandPaletteOpen: false,

  toggleFullscreen: () => set((s) => ({ isFullscreen: !s.isFullscreen })),
  toggleQuickNote: () => set((s) => ({ isQuickNoteOpen: !s.isQuickNoteOpen })),
  toggleSidebar: () => set((s) => ({ isSidebarCollapsed: !s.isSidebarCollapsed })),
  toggleCommandPalette: () => set((s) => ({ isCommandPaletteOpen: !s.isCommandPaletteOpen })),
  setCommandPaletteOpen: (open) => set({ isCommandPaletteOpen: open }),
}));
