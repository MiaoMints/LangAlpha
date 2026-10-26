import { useState } from 'react';
import { useSetMcpToolGroup } from '@/hooks/useMcpServers';
import { formatApiErrorDetail } from '@/pages/ChatAgent/utils/api';

export interface ToolGroupWrites {
  /** A group key, `null` for Off, or `TOOL_GROUP_AUTO` to follow the automatic grouping. */
  write: (tool: string, group: string | null) => Promise<void>;
  busy: (tool: string) => boolean;
  /** The server's refusal of the last change to this tool, verbatim. */
  refusalOn: (tool: string) => string | null;
}

/**
 * Per-tool saving state for group choices, so one tool saving does not hold
 * down the selects of the other eighty-seven, and a 409 lands under the tool
 * that was refused.
 */
export function useToolGroupWrites(serverName: string | null): ToolGroupWrites {
  const setGroup = useSetMcpToolGroup();
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const [failure, setFailure] = useState<{ tool: string; message: string } | null>(null);
  const write = async (tool: string, group: string | null) => {
    if (!serverName) return;
    setFailure(null);
    setPending((prev) => new Set(prev).add(tool));
    try {
      await setGroup.mutateAsync({ name: serverName, tool, group });
    } catch (err) {
      setFailure({ tool, message: formatApiErrorDetail(err) });
    } finally {
      setPending((prev) => {
        const next = new Set(prev);
        next.delete(tool);
        return next;
      });
    }
  };
  return {
    write,
    busy: (tool) => pending.has(tool),
    refusalOn: (tool) => (failure?.tool === tool ? failure.message : null),
  };
}
