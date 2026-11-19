import { useQuery } from '@tanstack/react-query';

export type Readiness = 'ready' | 'not_ready';

export const READINESS_URL = '/api/readyz';

/**
 * Asks the API whether it is ready. Only the status code is used: the body can list
 * extension versions and role names, so it is never read or shown.
 * A network failure rejects, so the query ends in the error state.
 */
export async function fetchReadiness(signal?: AbortSignal): Promise<Readiness> {
  const response = await fetch(READINESS_URL, signal ? { signal } : {});
  return response.status === 200 ? 'ready' : 'not_ready';
}

export function useReadiness() {
  return useQuery({
    queryKey: ['readiness'],
    queryFn: ({ signal }) => fetchReadiness(signal),
    refetchInterval: 15_000,
  });
}
