import { useQuery } from '@tanstack/react-query';
import { api } from '../client.ts';
import type { SystemStatsCountsDTO, TokenUsageResponse } from '../generated.ts';

export function useSystemStats() {
  return useQuery({
    queryKey: ['stats', 'counts'],
    queryFn: () => api.get<SystemStatsCountsDTO>('/stats/counts'),
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
