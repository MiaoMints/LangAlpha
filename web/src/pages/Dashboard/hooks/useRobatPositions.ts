import { queryOptions, useQuery } from '@tanstack/react-query';
import { getRobatPositions } from '../utils/robat';
import type { RobatPositionsPayload } from '../utils/robat';

/**
 * The console's own 30s cadence: positions change on fills and stops, not on
 * every tick, and the bridge makes a round trip to the API for each poll.
 */
export const robatPositionsQuery = queryOptions({
  queryKey: ['robatPositions'],
  queryFn: () => getRobatPositions(),
  staleTime: 1000 * 15,
  refetchInterval: 1000 * 30,
  refetchIntervalInBackground: false,
});

export interface RobatPositionsState {
  data: RobatPositionsPayload | undefined;
  rows: RobatPositionsPayload['positions'];
  loading: boolean;
  isError: boolean;
  status: string;
}

export function useRobatPositions(): RobatPositionsState {
  const query = useQuery(robatPositionsQuery);
  const data = query.data;
  const rows = data?.positions ?? [];
  const status = query.isError ? 'unavailable' : data?.status ?? (query.isLoading ? 'loading' : 'unknown');
  return {
    data,
    rows,
    loading: query.isLoading || (query.isFetching && !data),
    isError: query.isError,
    status,
  };
}
