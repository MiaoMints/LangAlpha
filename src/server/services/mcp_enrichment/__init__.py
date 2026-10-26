"""Model-written descriptions for MCP servers, and model-proposed groups for brokerage tools.

Two stages, one cache. The profile stage writes the one line a vendor's
``tools/list`` leaves out, what the server is for, and it only ever reaches the
server's row in the agent's tool manifest. The grouping stage labels what each
call of a shipped brokerage's uncurated tool does and proposes a capability
group for it; it reaches the gate only through the rules in ``assignments``,
which unlock a tool only when it reads, the deterministic rules agree, and the
model is confident. Neither the label nor the group is shown to the agent.
"""
