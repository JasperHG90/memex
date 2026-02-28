import { useMutation } from '@tanstack/react-query';
import { api } from '../client.ts';
import type { SummaryRequest, SummaryResponse } from '../generated.ts';

export function useSummary() {
  return useMutation({
    mutationFn: (request: SummaryRequest) =>
      api.post<SummaryResponse>('/memories/summary', request),
  });
}
