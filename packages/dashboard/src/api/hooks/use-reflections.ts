import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../client.ts';
import type { ReflectionResultDTO, ReflectionQueueDTO } from '../generated.ts';

export function useReflectionQueue() {
  return useQuery({
    queryKey: ['reflections', 'queue'],
    queryFn: () => api.get<ReflectionQueueDTO[]>('/reflections?status=queued&limit=200'),
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
