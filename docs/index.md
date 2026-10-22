# spider-hoarder documentation

Start with the [README](../README.md) (what it is, quick start, how to use it).

## Architecture
- [Overview](architecture/overview.md) — components, request flow, code map
- [Session isolation, egress and Tor](architecture/session-isolation.md) — uid pool, firewall, Tor, consent banners
- [Browser tool server](architecture/browser-toolserver.md) — multiple browsers per session, refs, text cap
- [Jobs, storage and links](architecture/jobs-and-storage.md) — job layout, HTML conversion, terse/simplified views, signed links
- [Authentication and live view](architecture/auth.md) — Keycloak, DCR, oauth2-proxy, human handoff
- [Scaling and routing](architecture/scaling.md) — nginx, replica ids, Tor port slices
- [Android emulators](architecture/android.md) — slots, wrapper, firewall, golden snapshot, the two Android servers

## API
- [Tool reference](api/tools.md)

## Operations
- [Deploying, configuring and testing](operations/deploy-and-configure.md)

## Plans (decision records)
- [Plans](plans/index.md) — open / background plans
- [Implemented plans](implemented-plans/index.md) — the plans that decided and built the above:
  [Browser sessions](implemented-plans/browser-sessions-plan.md),
  [Python browser tool server](implemented-plans/python-browser-tool-server.md),
  [Tool file outputs become jobs](implemented-plans/file-outputs-as-jobs.md),
  [save-page target](implemented-plans/save-page-target.md),
  [Jobs for all tools](implemented-plans/jobs-for-all-tools.md),
  [Terse view](implemented-plans/terse-view.md),
  [oauth2-proxy DCR](implemented-plans/oauth2-proxy-dcr.md),
  [Sticky replicas](implemented-plans/research-replicas.md),
  [Android hoarder](implemented-plans/android-hoarder.md)
