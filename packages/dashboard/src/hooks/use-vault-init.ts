import { useEffect } from 'react';
import { useDefaultVaults } from '@/api/hooks/use-vaults';
import { useVaultStore } from '@/stores/vault-store';

/**
 * Initializes the vault store from server defaults on app load.
 * Must be called once at the app root level.
 */
export function useVaultInit() {
  const { data: defaults } = useDefaultVaults();
  const isInitialized = useVaultStore((s) => s.isInitialized);
  const initialize = useVaultStore((s) => s.initialize);

  useEffect(() => {
    if (defaults && !isInitialized && defaults.length > 0) {
      const writer = defaults[0];
      const attached = defaults.slice(1).map((v) => ({ id: v.id, name: v.name }));
      initialize({ id: writer.id, name: writer.name }, attached);
    }
  }, [defaults, isInitialized, initialize]);
}
