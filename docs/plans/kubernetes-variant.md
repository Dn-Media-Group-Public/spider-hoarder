# Kubernetes variant

Status: stub, from the "Next steps" of the [Newsroom browsers plan](../implemented-plans/newsroom-browsers-plan.md). Not yet discussed; every design point below is an open question, not a decision. Current design: [Scale research containers](../implemented-plans/research-replicas.md), [Session isolation](../architecture/session-isolation.md).

## Problem / Goal

Run the stack on Kubernetes instead of Docker Swarm (original idea: a pod + service per session instead of a container).

## Open questions

- Is it wanted at all, given the Swarm replica design and `sessiond`?
- Scope: only the deployment (replicas, ingress, secrets), or also a different session isolation model (pod per session vs the uid pool)?
- What happens to `deploy.sh`, the nginx routing and the sticky-replica scheme?
