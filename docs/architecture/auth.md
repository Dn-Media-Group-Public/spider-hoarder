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
`read` (read, list, `get_file_text`, `grep_file`, `get_usage`, links), `write` (also create sessions and jobs, uploads) or `admin` (also add, change and
remove users, delete the project; a project always keeps at least one). Tools: `create_project`, `list_projects`, `get_project`,
`update_project`, `share_project`, `unshare_project`, `delete_project`; `open_session(project)` binds an MCP session to a project (needs `write`).
The gateway is the only component that reads or writes `access.json` and the `users/` index; "my projects" lists the index and keeps the
projects whose `access.json` still lists the caller, so a stale index entry grants nothing. Grant: write `access.json`, then the index entry;
revoke: `access.json`, then delete the entry. Removing a user takes effect within the 30 s cache; links and credentials already issued
live to their expiry. Access is by email: a reassigned address inherits access, and two admins saving at the same instant can lose one
change (fsspec has no conditional writes), both accepted. `project not found` is the answer both for a project that does not exist
and for one the caller has no role in. Deleting a project removes `access.json` first, then the index entries, ends the MCP sessions
bound to it and deletes the prefix, verifying afterwards that nothing is left (s3fs drops the per-key errors of a bulk delete).
Credentials minted before the delete can still write until they expire: orphans nobody can reach.
See also: [Plan: project access model](../implemented-plans/project-access-model.md)

### Policy levels
`access.json` also holds `policy: {<backend>: {<setting>: value | "ASK"}}`, the **project level** of the policy levels (admin env, project, session), set by `create_project(policy?)`
and, by an admin only, `update_project` (replaces it; an update replaces the local cache entry). Settings: `media` (bool), `allow` / `deny` (MIME lists), `ocr` / `asr` / `caption`
(ordered profile lists, `[]` = off; at most 1 profile for `ocr`, 2 for `asr` and `caption`: primary, then fallback). Each level may only **narrow** what the one above allows
(`media` true to false, `deny` grows, `allow` shrinks, a chain drops or reorders entries, never adds one) or say `ASK` (outside the order: the level below must choose, still bounded
by the nearest non-`ASK` value above; all `ASK` above = the setting's whole domain). A value outside its bound is rejected at the tool call, never clipped. The pure rules are
`gateway/policy.py`; `jobs.py` reads the admin level (`POLICY_<SETTING>` and `PROFILE_NAMES_<KIND>` in the gateway's environment, set from `<BACKEND>_POLICY_<SETTING>` by
`parse-overlay`), the project's section for this backend and the session's choices (`projects.session_policy`, dropped with the binding). A gateway only knows its own backend's
admin env, so only that backend's section of a project policy is fully validated on write; another backend's section is checked for shape, and a stored value that is
outside a bound later (the operator tightened it) or names a vanished profile is **dropped when resolving**, not rewritten. `get_project` returns the stored `policy` and, per
setting, the bound this backend's sessions get and its source. See also: [Plan: policy levels](../implemented-plans/policy-levels.md)

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
