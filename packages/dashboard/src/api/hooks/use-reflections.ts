import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../client.ts';
import type { ReflectionResultDTO, ReflectionQueueDTO } from '../generated.ts';

export function useReflectionQueue(vaultIds?: string[]) {
  return useQuery({
    queryKey: ['reflections', 'queue', vaultIds],
    queryFn: () => {
      const params = new URLSearchParams();
      params.set('status', 'queued');
      params.set('limit', '200');
      if (vaultIds?.length) {
        for (const id of vaultIds) params.append('vault_id', id);
      }
      return api.get<ReflectionQueueDTO[]>(`/reflections?${params}`);
    },
    refetchInterval: 30_000,
  });
}

export function useTriggerReflection() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (entityId: string) =>
      api.post<ReflectionResultDTO>('/reflections', { entity_id: entityId }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['reflections'] });
      void queryClient.invalidateQueries({ queryKey: ['entities'] });
    },
  });
}
