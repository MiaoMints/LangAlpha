import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { renderWithProviders } from '@/test/utils';
import { httpCatalogServer } from '@/test/factories';
import enUS from '@/locales/en-US.json';
import zhCN from '@/locales/zh-CN.json';
import type { CatalogServer, McpToolSummary } from '@/pages/ChatAgent/utils/api';
import type { Brokerage } from '../brokerages';

/**
 * Two things a brokerage's tool list says about grouping the tools nobody
 * curated: which ones the background service placed, and where the user may
 * move the read-only ones. The select is a write to a different route from the
 * binding control, and Automatic is the one value that is a DELETE rather than
 * a PUT, so each is asserted on the call it makes.
 */

const VENDOR: Brokerage = {
  name: 'moomoo',
  label: 'moomoo',
  url: 'https://mcp.example.com/moomoo',
  site: 'moomoo.com',
  description: '',
  native_callback_only: false,
  exclusive_connection: false,
  capabilities: [
    { key: 'market_data', tone: 'neutral' },
    { key: 'account', tone: 'caution' },
  ],
};

const base = { description: '', input_schema: {}, binding: 'ptc', binding_source: 'group' } as const;

const TOOLS: McpToolSummary[] = [
  {
    ...base,
    name: 'auto_placed_tool',
    capability: 'market_data',
    always_denied: false,
    capability_source: 'generated',
    effect: 'read',
    choosable: true,
    choice: null,
  },
  {
    ...base,
    name: 'curated_tool',
    capability: 'account',
    always_denied: false,
    capability_source: 'seed',
    effect: 'read',
    choosable: false,
    choice: null,
  },
  {
    ...base,
    name: 'user_placed_tool',
    capability: 'account',
    always_denied: false,
    capability_source: 'user',
    effect: 'read',
    choosable: true,
    choice: { group: 'account' },
  },
  {
    ...base,
    name: 'turned_off_tool',
    capability: null,
    always_denied: true,
    capability_source: 'user',
    effect: 'read',
    choosable: true,
    choice: { group: null },
  },
  { ...base, name: 'old_server_tool', capability: 'market_data', always_denied: false },
];

let tools: McpToolSummary[] = TOOLS;
let offered: string[] = ['market_data', 'account'];

vi.mock('@/api/tradingPermission', () => ({
  getTradingPermission: vi.fn(),
  updateTradingPermission: vi.fn(),
}));

const putGroup = vi.fn();
const deleteGroup = vi.fn();
vi.mock('@/pages/ChatAgent/utils/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/pages/ChatAgent/utils/api')>();
  return {
    ...actual,
    setMcpToolGroup: (...args: unknown[]) => putGroup(...args),
    clearMcpToolGroup: (...args: unknown[]) => deleteGroup(...args),
  };
});

vi.mock('@/hooks/useMcpServers', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/useMcpServers')>();
  return {
    ...actual,
    useMcpCatalogServerTools: () => ({
      data: { tools, order_modes: [], offered_groups: offered, discovered_at: null },
      isLoading: false,
      isError: false,
    }),
    useBuiltinMcpServerTools: () => ({ data: undefined, isLoading: false, isError: false }),
    useBrokerages: () => ({ data: [VENDOR], isLoading: false, error: null }),
  };
});

function row(): CatalogServer {
  return httpCatalogServer({
    name: 'moomoo',
    url: VENDOR.url,
    oauth_status: 'connected',
    granted_capabilities: ['market_data', 'account'],
    tool_binding: {},
    binding_preset: null,
    order_approval: { live: true, paper: false, staged: true },
    trading_permission: 'plan_first',
  });
}

async function renderDetail() {
  const { ServerDetail } = await import('../components/ServerDetail');
  const { DetailPageProvider } = await import('../components/DetailPage');
  return renderWithProviders(
    <DetailPageProvider value={{ section: 'Connectors' }}>
      <ServerDetail data={{ origin: 'brokerage', brokerage: VENDOR, server: row() }} onClose={() => {}} />
    </DetailPageProvider>,
  );
}

const groupSelect = (tool: string) =>
  screen.getByRole('combobox', { name: `Group for ${tool}` }) as HTMLSelectElement;

beforeEach(() => {
  putGroup.mockReset().mockResolvedValue({});
  deleteGroup.mockReset().mockResolvedValue({});
  tools = TOOLS;
  offered = ['market_data', 'account'];
});

describe('the grouped automatically marker', () => {
  it('marks a tool the grouping service placed, and only that one', async () => {
    await renderDetail();
    const marks = screen.getAllByText('Grouped automatically');

    expect(marks).toHaveLength(1);
    expect(marks[0]).toHaveAttribute(
      'title',
      "LangAlpha's grouping service put this read-only tool in this group. You can change it.",
    );
  });

  it('draws nothing on a server that predates the field', async () => {
    tools = [TOOLS[4]];
    await renderDetail();

    expect(screen.queryByText('Grouped automatically')).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });
});

describe('the group select', () => {
  it('is drawn only on a tool the server says the user may place', async () => {
    await renderDetail();

    expect(groupSelect('auto_placed_tool')).toBeInTheDocument();
    expect(groupSelect('user_placed_tool')).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Group for curated_tool' })).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Group for old_server_tool' })).not.toBeInTheDocument();
  });

  it('offers Automatic, each group the vendor offers, and Off', async () => {
    await renderDetail();
    const labels = within(groupSelect('auto_placed_tool'))
      .getAllByRole('option')
      .map((o) => o.textContent);

    expect(labels).toEqual(['Automatic', 'Market data and research', 'Account and positions', 'Off']);
  });

  it('reads no stored choice as Automatic, a group as that group, and null as Off', async () => {
    await renderDetail();

    expect(groupSelect('auto_placed_tool').value).toBe('auto');
    expect(groupSelect('user_placed_tool').value).toBe('account');
    expect(groupSelect('turned_off_tool').value).toBe('off');
  });

  it('keeps the way back on a tool that was turned off', async () => {
    await renderDetail();
    fireEvent.change(groupSelect('turned_off_tool'), { target: { value: 'market_data' } });

    await waitFor(() => expect(putGroup).toHaveBeenCalledWith('moomoo', 'turned_off_tool', 'market_data'));
  });

  it('puts a tool in the chosen group', async () => {
    await renderDetail();
    fireEvent.change(groupSelect('auto_placed_tool'), { target: { value: 'account' } });

    await waitFor(() => expect(putGroup).toHaveBeenCalledWith('moomoo', 'auto_placed_tool', 'account'));
    expect(deleteGroup).not.toHaveBeenCalled();
  });

  it('turns a tool off with a null group', async () => {
    await renderDetail();
    fireEvent.change(groupSelect('auto_placed_tool'), { target: { value: 'off' } });

    await waitFor(() => expect(putGroup).toHaveBeenCalledWith('moomoo', 'auto_placed_tool', null));
  });

  it('removes the stored choice for Automatic', async () => {
    await renderDetail();
    fireEvent.change(groupSelect('user_placed_tool'), { target: { value: 'auto' } });

    await waitFor(() => expect(deleteGroup).toHaveBeenCalledWith('moomoo', 'user_placed_tool'));
    expect(putGroup).not.toHaveBeenCalled();
  });

  it("shows the server's refusal under the tool that was refused", async () => {
    putGroup.mockRejectedValue({ response: { status: 409, data: { detail: 'Not allowed for this tool' } } });
    await renderDetail();
    fireEvent.change(groupSelect('auto_placed_tool'), { target: { value: 'account' } });

    expect(await screen.findByRole('alert')).toHaveTextContent('Not allowed for this tool');
  });
});

describe('copy', () => {
  it('has both locales for every string the control draws', () => {
    for (const locale of [enUS, zhCN]) {
      const detail = locale.plugins.detail as Record<string, string>;
      for (const key of ['groupedAuto', 'groupedAutoTitle', 'groupAria', 'groupAuto', 'groupOff']) {
        expect(detail[key]).toBeTruthy();
        expect(detail[key]).not.toMatch(/[\u2013\u2014]/);
      }
    }
  });
});
