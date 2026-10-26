import { describe, it, expect, vi, beforeEach } from 'vitest';

const { put, del, get } = vi.hoisted(() => ({ put: vi.fn(), del: vi.fn(), get: vi.fn() }));
vi.mock('@/api/client', () => ({ api: { put, delete: del, get, defaults: {} } }));

import {
  clearMcpToolGroup,
  getMcpCatalogServerTools,
  setMcpToolGroup,
} from '@/pages/ChatAgent/utils/api';

beforeEach(() => {
  put.mockReset().mockResolvedValue({ data: { tool: 'a b', choice: { group: 'account' } } });
  del.mockReset().mockResolvedValue({ data: { tool: 'a b', choice: null } });
  get.mockReset();
});

describe('the tool group routes', () => {
  it('PUTs the group, and null for Off', async () => {
    await setMcpToolGroup('moomoo', 'a b', 'account');
    expect(put).toHaveBeenLastCalledWith('/api/v1/mcp/servers/moomoo/tools/a%20b/group', { group: 'account' });

    await setMcpToolGroup('moomoo', 'a b', null);
    expect(put).toHaveBeenLastCalledWith('/api/v1/mcp/servers/moomoo/tools/a%20b/group', { group: null });
  });

  it('DELETEs the choice to return to Automatic', async () => {
    await clearMcpToolGroup('moomoo', 'a b');
    expect(del).toHaveBeenCalledWith('/api/v1/mcp/servers/moomoo/tools/a%20b/group');
  });

  it('reads a server that predates grouping as offering nothing', async () => {
    get.mockResolvedValue({ data: { server_name: 'moomoo', tools: [] } });
    const result = await getMcpCatalogServerTools('moomoo');
    expect(result.offered_groups).toEqual([]);
  });
});
