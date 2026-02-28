import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export interface PreferencesStore {
  defaultSearchLimit: number;
  defaultStrategies: string[];
  autoRefreshInterval: number; // 0 = off, otherwise seconds
  sidebarCollapsedByDefault: boolean;

  setDefaultSearchLimit: (limit: number) => void;
  setDefaultStrategies: (strategies: string[]) => void;
  setAutoRefreshInterval: (seconds: number) => void;
  setSidebarCollapsedByDefault: (collapsed: boolean) => void;
}

export const usePreferencesStore = create<PreferencesStore>()(
  persist(
    (set) => ({
      defaultSearchLimit: 10,
      defaultStrategies: ['semantic', 'keyword', 'graph', 'temporal', 'mental_model'],
      autoRefreshInterval: 30,
      sidebarCollapsedByDefault: false,

      setDefaultSearchLimit: (limit) => set({ defaultSearchLimit: limit }),
      setDefaultStrategies: (strategies) => set({ defaultStrategies: strategies }),
      setAutoRefreshInterval: (seconds) => set({ autoRefreshInterval: seconds }),
      setSidebarCollapsedByDefault: (collapsed) => set({ sidebarCollapsedByDefault: collapsed }),
    }),
    {
      name: 'memex_preferences',
    },
  ),
);
