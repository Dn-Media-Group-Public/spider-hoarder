"""The driver seam: everything the gateway framework (app.py, sessions.py) needs from a backend.

A backend package registers a descriptor under the entry-point group `spider_hoarder.backends` (see
docs/architecture/backend-plugins.md); the descriptor's `runtime` member is a `module:Object` import string naming the
driver. The gateway imports it lazily, inside the container only, for the backend named by $BACKEND.

A driver subclasses Driver and overrides what differs. It must not import sessions.py or app.py at module level (they
import the driver); import them inside the methods that need them."""

import importlib
import importlib.metadata
import os

import mcp.types as types

ENTRY_GROUP = "spider_hoarder.backends"


class _LazySessions:
    """The session registry, imported on first use: it imports the driver, which imports the driver's module."""

    def __getattr__(self, name):
        from spider_hoarder_sdk.gateway import sessions as _s
        return getattr(_s, name)


sessions = _LazySessions()  # drivers: `from spider_hoarder_sdk.gateway.driver import sessions`


def hyphen(name: str) -> str:
    """Canonical id (`browser_tor`) -> URL prefix, Swarm service and DNS form (`browser-tor`)."""
    return name.replace("_", "-")


class Driver:
    name: str = ""             # canonical id, set by the gateway from $BACKEND when the driver leaves it empty
    noun = "session"           # for user-facing messages: "browser", "device"
    idle_timeout = 1800        # seconds without a tool call before the session is reaped
    help_timeout = 3600        # a pending human-help request suspends the idle timer, up to this
    max_per_user = 1
    max_total: int | None = None   # per-replica session cap checked at reserve; None = the pool's size is the cap
    close_timeout = 60.0       # how long close() waits for the session task (must outlast a slot reset)
    instructions = ""          # the backend's own note for the LLM (the framework adds the other-servers list)
    file_tools: dict[str, str] = {}      # proxied tools whose output is a file in a job: name -> default file name
    capped_tools: tuple = ()             # of those, the ones that take `text_cap`
    hidden: frozenset = frozenset()      # upstream tools never exposed
    extra_tools: list[types.Tool] = []   # the gateway's own tools besides the job tools (open_session, ...)

    @property
    def prefix(self) -> str:
        return hyphen(self.name)

    # ---- pool and upstream session
    def make_pool(self):
        """The spider_hoarder_sdk.gateway.pool.Pool of this replica (backend start/stop/reset + the uids it owns)."""
        raise NotImplementedError

    async def prepare(self, bs) -> tuple[dict, dict]:
        """(env, files) for pool.start; may raise to refuse the start."""
        return {}, {}

    def upstream_mcp(self, uid: int) -> tuple[str, dict, dict]:
        """(url, headers, kwargs for streamablehttp_client) of the session's tool server."""
        raise NotImplementedError

    async def after_initialize(self, cs, bs) -> None:
        """Right after the upstream MCP handshake (browser: install the media policy)."""

    def view_upstream(self, uid: int, rid: str) -> str:
        """`host:port` nginx proxies the live view (noVNC page and websocket) of a session to; answered by /authz."""
        raise NotImplementedError

    # ---- tools
    async def upstream_tools(self) -> list[types.Tool]:
        """The tool server's tool list (a static list, or fetched once from a throwaway session)."""
        raise NotImplementedError

    def decorate_tools(self, tools: list[types.Tool]) -> list[types.Tool]:
        """Adjust the (hidden-filtered) upstream tools: notes, schemas."""
        return tools

    def known(self, name: str) -> bool:
        """False = reject an unknown tool before it books a session."""
        return True

    async def pre_session(self, name: str, args: dict, ctx):
        """Tools and checks that need no session, run before the job tools. Return a CallToolResult to answer, or None."""

    async def with_session(self, name: str, args: dict, ctx, bs):
        """Tools that need the session (bs), run before the generic proxy. Return a CallToolResult, or None to proxy."""

    async def file_tool(self, bs, name: str, args: dict):
        """Run one of `file_tools`; the output must end up under a new job id. Return the CallToolResult."""
        raise NotImplementedError

    def register(self, mcp) -> None:
        """Optional extra routes: mcp.custom_route(...)."""


def load(name: str | None = None) -> Driver:
    name = name or os.environ.get("BACKEND", "")
    if not name:
        raise RuntimeError("BACKEND is not set (the canonical name of the backend this gateway serves, e.g. browser_tor)")
    eps = importlib.metadata.entry_points(group=ENTRY_GROUP, name=name)
    if not eps:
        raise RuntimeError(f"backend {name!r} is not installed (no entry point {name!r} in group {ENTRY_GROUP})")
    descriptor = next(iter(eps)).load()
    mod, _, attr = descriptor.runtime.partition(":")
    d = getattr(importlib.import_module(mod), attr)
    d.name = d.name or name
    return d


driver = load()
