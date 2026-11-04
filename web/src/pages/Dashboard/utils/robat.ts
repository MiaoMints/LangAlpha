/**
 * ROBAT positions, served by the console bridge at /api/v1/robat/positions.
 *
 * The bridge never raises: a missing token or an unreachable console comes back
 * as a payload whose `status` says so, which lets the card print a reason
 * instead of a stack trace.
 */
import { api } from '@/api/client';

export interface RobatTpStep {
  name?: string | null;
  target_price?: number | null;
  planned_qty?: number | null;
  filled_qty?: number | null;
  status?: string | null;
  algo_status?: string | null;
}

export interface RobatPosition {
  symbol: string;
  side?: string | null;
  state?: string | null;
  quantity?: number | null;
  entry?: number | null;
  mark?: number | null;
  take_profit?: number | null;
  stop_loss?: number | null;
  unrealized_pnl?: number | null;
  realized_pnl?: number | null;
  r_multiple?: number | null;
  risk_budget_usdt?: number | null;
  margin_usdt?: number | null;
  notional_usdt?: number | null;
  leverage?: number | null;
  trader_name?: string | null;
  tp_status?: string | null;
  sl_status?: string | null;
  tp_steps?: RobatTpStep[] | null;
  updated_at?: string | null;
}

export interface RobatPositionsPayload {
  status: string;
  error: string;
  count: number;
  total_count: number;
  positions: RobatPosition[];
}

const EMPTY: RobatPositionsPayload = { status: 'unknown', error: '', count: 0, total_count: 0, positions: [] };

export async function getRobatPositions(page = 1, pageSize = 50): Promise<RobatPositionsPayload> {
  const { data } = await api.get('/api/v1/robat/positions', {
    params: { page, page_size: pageSize },
    timeout: 15000,
  });
  const payload = data as Partial<RobatPositionsPayload> | null | undefined;
  if (!payload || typeof payload !== 'object') return { ...EMPTY, status: 'invalid_json' };
  return {
    status: String(payload.status ?? 'unknown'),
    error: String(payload.error ?? ''),
    count: Number(payload.count ?? 0),
    total_count: Number(payload.total_count ?? 0),
    positions: Array.isArray(payload.positions) ? payload.positions : [],
  };
}
