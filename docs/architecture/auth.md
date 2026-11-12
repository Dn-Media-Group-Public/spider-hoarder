# Authentication and live view

## MCP (Claude)
Keycloak (any OIDC) JWT on `/<backend>/mcp` (e.g. `/browser-tor/mcp`) (`spider_hoarder_sdk/gateway/auth.py`): issuer, signature, `exp`; the `email` claim identifies the owner; if
`MCP_SCOPE` is set the token's `scope` must contain it (the audience is deliberately **not** checked: Claude's dynamically
registered clients carry arbitrary audiences). RFC 9728 protected-resource metadata advertises the scope so Claude requests it.
Claude registers itself through anonymous Dynamic Client Registration, so there is no client id/secret to configure.
`keycloak/setup.sh` (idempotent) creates: the realm if missing, the realm-default scope, DCR limited by the Trusted Hosts policy to `claude.ai`,
`claude.com` and `localhost`, and one user. See also: [Plan: Browser sessions](../implemented-plans/browser-sessions-plan.md)
(implementation notes) and the `mcp-development:mcp-fastapi-oauth-dcr` pattern it follows.

## Projects and roles
Every job lives in a project. Any authenticated user can create one and becomes its admin; `access.json` maps lowercased emails to
`read` (read, list, `get_file_text`, `grep_file`, links), `write` (also create sessions and jobs, uploads) or `admin` (also add, change and
remove users, delete the project; a project always keeps at least one). Tools: `create_project`, `list_projects`, `get_project`,
`share_project`, `unshare_project`, `delete_project`; `open_session(project)` binds an MCP session to a project (needs `write`).
The gateway is the only component that reads or writes `access.json` and the `users/` index; "my projects" lists the index and keeps the
projects whose `access.json` still lists the caller, so a stale index entry grants nothing. Grant: write `access.json`, then the index entry;
revoke: `access.json`, then delete the entry. Removing a user takes effect within the 30 s cache; links and credentials already issued
live to their expiry. Access is by email: a reassigned address inherits access, and two admins saving at the same instant can lose one
change (fsspec has no conditional writes), both accepted. `project not found` is the answer both for a project that does not exist
and for one the caller has no role in. Deleting a project removes `access.json` first, then the index entries, ends the MCP sessions
bound to it and deletes the prefix, verifying afterwards that nothing is left (s3fs drops the per-key errors of a bulk delete).
Credentials minted before the delete can still write until they expire: orphans nobody can reach.
See also: [Plan: project access model](../implemented-plans/project-access-model.md)

## Live view (human handoff)
`browser_request_human_help` returns `…/<egress>/view/<rid>/<view_id>/start`. The human opens it, signs in via oauth2-proxy (same
Keycloak account), and nginx runs two `auth_request` stages: SSO (sets the email, client-supplied values dropped), then the replica's
`/authz`, which checks that the SSO email owns the session and answers `X-Upstream` (its own IP and websockify port; client-supplied
`X-Upstream` is never read). `/start` redirects to noVNC with the session's VNC password. Non-owners get 403.
See also: [Plan: Scale research containers](../implemented-plans/research-replicas.md) (two-stage nginx, `rid` in the URL)

## oauth2-proxy registers itself (DCR)
A wrapper entrypoint (`oauth2-proxy/entrypoint.sh`, alpine variant of the image + `jq` + GNU `wget`; curl would ignore the dev `/etc/hosts` rewrite for `localhost`) registers a client at
`$OIDC_ISSUER/clients-registrations/openid-connect`, persists it in the `o2p_state` volume, reuses it across restarts (validated with the
registration access token), replaces it when `PUBLIC_HOST`/scheme change, and fails loudly if registration is rejected. Single replica.
See also: [Plan: oauth2-proxy DCR](../implemented-plans/oauth2-proxy-dcr.md)

## TLS
TLS ends at the Google load balancer; the stack only speaks http. `PUBLIC_SCHEME` tells services which URLs users see; the LB must
forward `X-Forwarded-Proto`; `/healthz` answers 200.

See also: [Plan: Pluggable backends](../implemented-plans/backend-plugins.md) (the same auth framework serves every backend)
