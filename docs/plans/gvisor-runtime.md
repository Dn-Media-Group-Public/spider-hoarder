# gVisor runtime for session containers

Status: stub, from the "Next steps" of the [Newsroom browsers plan](../implemented-plans/newsroom-browsers-plan.md). Not yet discussed; every design point below is an open question, not a decision. Current design: [Browser sessions](../implemented-plans/browser-sessions-plan.md), [Session isolation](../architecture/session-isolation.md).

## Problem / Goal

Stronger isolation of hostile pages than the plain container boundary (original idea: `runtime="runsc"`).

## Open questions

- Does it work with the current setup: Swarm services (no per-service runtime?), `sessiond` uid pool, Chromium, the per-uid firewall, the Android-hoarder slots?
- Which services get it (`research-*` only)? Performance and capacity cost?
- Host prerequisite and how `deploy.sh` handles it.
