# Persistent profile and Playwright traces

Status: stub, from the "Next steps" of the [Newsroom browsers plan](../implemented-plans/newsroom-browsers-plan.md). Not yet discussed; every design point below is an open question, not a decision. Current design: [Browser sessions](../implemented-plans/browser-sessions-plan.md), [Session isolation](../architecture/session-isolation.md).

## Problem / Goal

Two optional browser-session extras from the original plan: a per-user persistent browser profile, and per-session Playwright traces as an evidence trail.

## Open questions

- Persistent profile: where stored, per-user keying, Chrome profile locks with concurrent sessions, interplay with the 'wiped between sessions' isolation model.
- Traces: stored as job files? size and retention? how requested?
- Should these be split into separate plans once one is picked?
