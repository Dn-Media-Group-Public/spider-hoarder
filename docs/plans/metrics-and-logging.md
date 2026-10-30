# Metrics and structured logs

Status: stub, from the "Next steps" of the [Newsroom browsers plan](../implemented-plans/newsroom-browsers-plan.md). Not yet discussed; every design point below is an open question, not a decision. Current design: [Browser sessions](../implemented-plans/browser-sessions-plan.md), [Session isolation](../architecture/session-isolation.md).

## Problem / Goal

Visibility into session start/stop, handoffs, capacity and failures (original idea: Prometheus metrics / structured logs).

## Open questions

- Metrics endpoint vs structured logs only; which system consumes them?
- Which events/gauges (sessions per replica, handoffs, reaps, errors)?
- Where is it exposed given the nginx/auth layout (not publicly)?
