import { useQuery } from '@tanstack/react-query';
import { api } from '../client.ts';
import type { SystemStatsCountsDTO, TokenUsageResponse } from '../generated.ts';

export function useSystemStats(vaultIds?: string[]) {
  return useQuery({
    queryKey: ['stats', 'counts', vaultIds],
    queryFn: () => {
      const params = new URLSearchParams();
      if (vaultIds?.length) {
        for (const id of vaultIds) params.append('vault_id', id);
      }
      const qs = params.toString();
      return api.get<SystemStatsCountsDTO>(`/stats/counts${qs ? `?${qs}` : ''}`);
    },
  });
}

export function useTokenUsage() {
  return useQuery({
    queryKey: ['stats', 'token-usage'],
    queryFn: () => api.get<TokenUsageResponse>('/stats/token-usage'),
  });
}

export function useMetrics() {
  return useQuery({
    queryKey: ['metrics'],
    queryFn: async () => {
      const response = await api.getRaw('/metrics');
      return response.text();
    },
    refetchInterval: 5000,
  });
}
