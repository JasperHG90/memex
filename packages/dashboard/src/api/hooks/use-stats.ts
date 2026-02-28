import { useQuery } from '@tanstack/react-query';
import { api } from '../client.ts';
import { SystemStatsCountsDTO, TokenUsageResponse } from '../generated.ts';
import { validateResponse } from '../validate.ts';

export function useSystemStats(vaultIds?: string[]) {
  return useQuery({
    queryKey: ['stats', 'counts', vaultIds],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (vaultIds?.length) {
        for (const id of vaultIds) params.append('vault_id', id);
      }
      const qs = params.toString();
      const data = await api.get<SystemStatsCountsDTO>(`/stats/counts${qs ? `?${qs}` : ''}`);
      return validateResponse(SystemStatsCountsDTO, data);
    },
  });
}

export function useTokenUsage() {
  return useQuery({
    queryKey: ['stats', 'token-usage'],
    queryFn: async () => {
      const data = await api.get<TokenUsageResponse>('/stats/token-usage');
      return validateResponse(TokenUsageResponse, data);
    },
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
