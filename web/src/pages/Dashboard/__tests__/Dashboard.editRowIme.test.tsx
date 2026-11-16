import { Suspense, useSyncExternalStore, type ReactNode } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import { QueryClientProvider, QueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { queryKeys } from '@/lib/queryKeys';

const h = vi.hoisted(() => ({ handleUpdate: vi.fn() }));

const prefsState = {
  current: { other_preference: { theme: 'dark' } } as
    | { other_preference?: Record<string, unknown> | null }
    | null,
};
const loadingState = { isLoading: false, failed: false };
let mockIsMobile = false;
const mobileListeners = new Set<() => void>();

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () =>
    useSyncExternalStore(
      (onChange) => {
        mobileListeners.add(onChange);
        return () => mobileListeners.delete(onChange);
      },
      () => mockIsMobile,
    ),
}));
vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({
    preferences: prefsState.current,
    isLoading: loadingState.isLoading,
    isLoaded: !loadingState.isLoading && !loadingState.failed,
  }),
}));
vi.mock('@/hooks/useUpdatePreferences', () => ({
  PREFERENCE_MUTATION_KEY: ['user-preferences'],
  useUpdatePreferences: () => ({
    mutateAsync: () => new Promise(() => {}),
    isPending: false,
  }),
}));
vi.mock('../utils/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/api')>()),
  getPreferences: () => new Promise(() => {}),
}));
vi.mock('../widgets/framework/DashboardDataContext', () => ({
  DashboardDataProvider: ({ children }: { children: ReactNode }) => children,
  useDashboardContext: () => ({
    portfolio: {
      editRow: { symbol: 'AAPL' },
      editForm: { quantity: '1' },
      openEdit: vi.fn(),
      setEditForm: vi.fn(),
      modalOpen: false,
      setModalOpen: vi.fn(),
      handleUpdate: h.handleUpdate,
    },
    watchlist: { modalOpen: false, setModalOpen: vi.fn(), currentWatchlistId: null },
    modals: {
      selectedNewsId: null,
      selectedMarketInsightId: null,
      closeNews: vi.fn(),
      closeInsight: vi.fn(),
      deleteConfirm: { open: false, title: '', message: '' },
      runDeleteConfirm: vi.fn(),
      cancelDeleteConfirm: vi.fn(),
    },
  }),
}));
vi.mock('@/pages/MarketView/contexts/MarketDataWSContext', () => ({
  MarketDataWSProvider: ({ children }: { children: ReactNode }) => children,
}));
vi.mock('../hooks/useOnboarding', () => ({
  useOnboarding: () => ({ showPersonalizationBanner: false, setShowPersonalizationBanner: vi.fn() }),
  snoozePersonalization: vi.fn(),
}));
vi.mock('../components/DashboardHeader', () => ({ default: () => <div data-testid="dashboard-header" /> }));
vi.mock('../components/ChatInputCard', () => ({ default: () => null }));
vi.mock('../components/NewsDetailModal', () => ({ default: () => null }));
vi.mock('../components/InsightDetailModal', () => ({ default: () => null }));
vi.mock('../components/AddWatchlistItemDialog', () => ({ default: () => null }));
vi.mock('../components/AddPortfolioHoldingDialog', () => ({ default: () => null }));
vi.mock('../widgets/framework/AddWidgetDialog', () => ({ AddWidgetDialog: () => null }));
vi.mock('../widgets/framework/PresetsDialog', () => ({ PresetsDialog: () => null }));
vi.mock('../widgets/framework/DashboardGrid', () => ({ DashboardGrid: () => <div data-testid="dashboard-grid" /> }));
vi.mock('../widgets/framework/DashboardStack', () => ({ DashboardStack: () => <div data-testid="dashboard-stack" /> }));

import Dashboard from '../Dashboard';

function makeClient() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
  qc.setQueryData(queryKeys.user.preferences(), prefsState.current);
  return qc;
}

describe('Dashboard portfolio edit-row IME guard', () => {
  beforeEach(() => {
    h.handleUpdate.mockReset();
    loadingState.isLoading = false;
    loadingState.failed = false;
  });

  it('does not call handleUpdate while composing, calls it on a plain Enter', () => {
    render(
      <QueryClientProvider client={makeClient()}>
        <MemoryRouter>
          <Suspense fallback={<div />}>
            <Dashboard />
          </Suspense>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const input = document.querySelector('input[type="number"]') as HTMLInputElement;
    expect(input).toBeTruthy();
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    expect(h.handleUpdate).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(h.handleUpdate).toHaveBeenCalledTimes(1);
  });
});
