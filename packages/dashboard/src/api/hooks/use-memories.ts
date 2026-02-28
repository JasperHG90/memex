import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';
import { api } from '../client.ts';
import {
  MemoryUnitDTO,
  LineageResponse,
  type RetrievalRequest,
} from '../generated.ts';
import { validateResponse } from '../validate.ts';

const MemoryUnitArraySchema = z.array(MemoryUnitDTO);

export function useMemory(unitId: string | undefined) {
  return useQuery({
    queryKey: ['memories', unitId],
    queryFn: async () => {
      const data = await api.get<MemoryUnitDTO>(`/memories/${unitId}`);
      return validateResponse(MemoryUnitDTO, data);
    },
    enabled: !!unitId,
  });
}

export function useMemorySearch() {
  return useMutation({
    mutationFn: async (request: RetrievalRequest) => {
      const data = await api.post<MemoryUnitDTO[]>('/memories/search', request);
      return validateResponse(MemoryUnitArraySchema, data);
    },
  });
}

export function useAdjustBelief() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ unitId, adjustment }: { unitId: string; adjustment: 'confirm' | 'contradict' }) => {
      const evidence_type_key = adjustment === 'confirm' ? 'user_validation' : 'user_rejection';
      return api.patch<void>(`/memories/${unitId}/belief`, {
        unit_uuid: unitId,
        evidence_type_key,
        description: `User ${adjustment}ed this fact via dashboard.`,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['memories'] });
    },
  });
}

export function useDeleteMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (unitId: string) => api.delete<void>(`/memories/${unitId}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['memories'] });
      void queryClient.invalidateQueries({ queryKey: ['stats'] });
    },
  });
}

export function useMemoryLineage(
  unitId: string | undefined,
  options?: { direction?: string; depth?: number; limit?: number },
) {
  const params = new URLSearchParams();
  if (options?.direction) params.set('direction', options.direction);
  if (options?.depth != null) params.set('depth', String(options.depth));
  if (options?.limit != null) params.set('limit', String(options.limit));
  const qs = params.toString();

  return useQuery({
    queryKey: ['memories', unitId, 'lineage', options],
    queryFn: () =>
      api.get<LineageResponse>(`/notes/${unitId}/lineage${qs ? `?${qs}` : ''}`),
    enabled: !!unitId,
  });
}
