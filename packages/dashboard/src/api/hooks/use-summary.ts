import { useMutation } from '@tanstack/react-query';
import { api } from '../client.ts';
import { SummaryResponse, type SummaryRequest } from '../generated.ts';
import { validateResponse } from '../validate.ts';

export function useSummary() {
  return useMutation({
    mutationFn: async (request: SummaryRequest) => {
      const data = await api.post<SummaryResponse>('/memories/summary', request);
      return validateResponse(SummaryResponse, data);
    },
  });
}
