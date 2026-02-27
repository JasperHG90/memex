import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../client.ts';
import type { VaultDTO, CreateVaultRequest } from '../generated.ts';

export function useVaults() {
  return useQuery({
    queryKey: ['vaults'],
    queryFn: () => api.get<VaultDTO[]>('/vaults'),
  });
}

export function useDefaultVaults() {
  return useQuery({
    queryKey: ['vaults', 'defaults'],
    queryFn: () => api.get<VaultDTO[]>('/vaults?is_default=true'),
  });
}

export function useActiveVault() {
  return useQuery({
    queryKey: ['vaults', 'active'],
    queryFn: () => api.get<VaultDTO[]>('/vaults?state=active'),
    select: (data) => data[0],
  });
}

export function useCreateVault() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: CreateVaultRequest) =>
      api.post<VaultDTO>('/vaults', request),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['vaults'] });
    },
  });
}

export function useDeleteVault() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vaultId: string) =>
      api.delete<{ status: string }>(`/vaults/${vaultId}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['vaults'] });
    },
  });
}
