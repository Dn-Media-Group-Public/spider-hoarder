# Port to the `mcp` 2.x SDK

Status: stub, from the "Next steps" of the [Newsroom browsers plan](../implemented-plans/newsroom-browsers-plan.md). Not yet discussed; every design point below is an open question, not a decision. Current design: [Browser sessions](../implemented-plans/browser-sessions-plan.md), [Session isolation](../architecture/session-isolation.md).

## Problem / Goal

The gateway and tool servers use the v1 API (FastMCP, low-level handlers); 2.x renamed FastMCP to `MCPServer`.

## Open questions

- Is there a reason to move now, or only when a needed fix lands in 2.x?
- Which pins (`mcp==...`, `@playwright/mcp`) move together, and how is the proxy/low-level handler override affected?
- Test coverage needed before bumping.
