import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';
import { api } from '../client.ts';
import { VaultDTO, type CreateVaultRequest } from '../generated.ts';
import { validateResponse } from '../validate.ts';

const VaultArraySchema = z.array(VaultDTO);

export function useVaults() {
  return useQuery({
    queryKey: ['vaults'],
    queryFn: async () => {
      const data = await api.get<VaultDTO[]>('/vaults');
      return validateResponse(VaultArraySchema, data);
    },
  });
}

export function useDefaultVaults() {
  return useQuery({
    queryKey: ['vaults', 'defaults'],
    queryFn: async () => {
      const data = await api.get<VaultDTO[]>('/vaults?is_default=true');
      return validateResponse(VaultArraySchema, data);
    },
  });
}

export function useActiveVault() {
  return useQuery({
    queryKey: ['vaults', 'active'],
    queryFn: async () => {
      const data = await api.get<VaultDTO[]>('/vaults?state=active');
      return validateResponse(VaultArraySchema, data);
    },
    select: (data) => data[0],
  });
}

export function useCreateVault() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (request: CreateVaultRequest) => {
      const data = await api.post<VaultDTO>('/vaults', request);
      return validateResponse(VaultDTO, data);
    },
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
