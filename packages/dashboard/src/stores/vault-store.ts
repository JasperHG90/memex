import { create } from 'zustand';

interface AttachedVault {
  id: string;
  name: string;
}

interface VaultStore {
  writerVaultId: string;
  writerVaultName: string;
  attachedVaults: AttachedVault[];
  isInitialized: boolean;

  allSelectedVaultIds: () => string[];
  setWriterVault: (id: string, name: string) => void;
  toggleAttachedVault: (id: string, name: string, checked: boolean) => void;
  initialize: (writerVault: { id: string; name: string }, attached: AttachedVault[]) => void;
}

export const useVaultStore = create<VaultStore>((set, get) => ({
  writerVaultId: '',
  writerVaultName: '',
  attachedVaults: [],
  isInitialized: false,

  allSelectedVaultIds: () => {
    const state = get();
    const ids = new Set<string>();
    if (state.writerVaultId) ids.add(state.writerVaultId);
    for (const v of state.attachedVaults) ids.add(v.id);
    return [...ids];
  },

  setWriterVault: (id, name) =>
    set((state) => ({
      writerVaultId: id,
      writerVaultName: name,
      attachedVaults: state.attachedVaults.filter((v) => v.id !== id),
    })),

  toggleAttachedVault: (id, name, checked) =>
    set((state) => ({
      attachedVaults: checked
        ? state.attachedVaults.some((v) => v.id === id)
          ? state.attachedVaults
          : [...state.attachedVaults, { id, name }]
        : state.attachedVaults.filter((v) => v.id !== id),
    })),

  initialize: (writerVault, attached) =>
    set({
      writerVaultId: writerVault.id,
      writerVaultName: writerVault.name,
      attachedVaults: attached,
      isInitialized: true,
    }),
}));
