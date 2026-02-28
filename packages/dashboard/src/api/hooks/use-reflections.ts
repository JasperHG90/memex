import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';
import { api } from '../client.ts';
import { ReflectionResultDTO, ReflectionQueueDTO } from '../generated.ts';
import { validateResponse } from '../validate.ts';

const ReflectionQueueArraySchema = z.array(ReflectionQueueDTO);

export function useReflectionQueue(vaultIds?: string[]) {
  return useQuery({
    queryKey: ['reflections', 'queue', vaultIds],
    queryFn: async () => {
      const params = new URLSearchParams();
      params.set('status', 'queued');
      params.set('limit', '200');
      if (vaultIds?.length) {
        for (const id of vaultIds) params.append('vault_id', id);
      }
      const data = await api.get<z.infer<typeof ReflectionQueueDTO>[]>(`/reflections?${params}`);
      return validateResponse(ReflectionQueueArraySchema, data);
    },
    refetchInterval: 30_000,
  });
}

export function useTriggerReflection() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (entityId: string) => {
      const data = await api.post<z.infer<typeof ReflectionResultDTO>>('/reflections', { entity_id: entityId });
      return validateResponse(ReflectionResultDTO, data);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['reflections'] });
      void queryClient.invalidateQueries({ queryKey: ['entities'] });
    },
  });
}
