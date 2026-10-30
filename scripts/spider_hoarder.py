#!/usr/bin/env python3
"""Deploy tooling of spider-hoarder, run by deploy.sh inside the virtualenv .venv/ (standalone: not part of any package).

Backends register a descriptor under the entry-point group `spider_hoarder.backends`; $BACKENDS (space-separated canonical
names, e.g. `browser_clear browser_tor android_tor`) says which are enabled. Everything here reads the installed
descriptors with importlib.metadata and uses PyYAML for the overlay check; see docs/architecture/backend-plugins.md.

  validate           every name in BACKENDS is installed, descriptors are well formed, overlays obey the contract
  preflight          run each backend's preflight script (non-zero = abort, with its stderr)
  build WHEELS       docker build every image of the enabled backends, once per tag (first descriptor wins)
  post-build         run each backend's post_build script
  env                `export K=V` lines: every backend's env(environ) result, BACKENDS and BACKEND_SERVERS
  landing FILE       the static landing page (claude mcp add lines + each backend's title and description)
  compose            one `-c <overlay>` argument per line
  services           the services to force-restart after deploy, one per line (unprefixed)
  dev-overlay FILE   compose overlay with the dev-only settings of every enabled gateway

Hooks (preflight, post_build, build contexts) get BACKEND, BACKEND_HYPHEN and STACK_NAME in their environment."""

import html
import importlib
import importlib.metadata
import json
import os
import re
import shlex
import subprocess
import sys

GROUP = "spider_hoarder.backends"
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")
MEMBERS = ("name", "title", "description", "compose", "builds", "services", "runtime")  # required; the rest are optional
OPTIONAL = ("preflight", "post_build", "env")
SHARED_NETWORKS = ("public", "auth")  # the base compose file's networks, usable by overlays


class DeployError(Exception):
    pass


def hyphen(name: str) -> str:
    return name.replace("_", "-")


class Backend:
    """A resolved descriptor: the duck-typed object plus the directory of the package it came from."""

    def __init__(self, ep_name: str, descriptor, pkg_dir: str):
        self.d, self.dir, self.ep_name = descriptor, pkg_dir, ep_name
        self.name = descriptor.name
        self.h = hyphen(self.name)

    def path(self, rel: str) -> str:
        return os.path.join(self.dir, rel)  # an absolute rel (a family package's path) wins

    def optional(self, member):
        return getattr(self.d, member, None)


def entry_points():
    return importlib.metadata.entry_points(group=GROUP)


def package_dir(ep) -> str:
    """Directory of the (top-level) package the entry point's module lives in: descriptor paths are relative to it."""
    top = ep.value.split(":")[0].split(".")[0]
    mod = importlib.import_module(top)
    return os.path.dirname(os.path.abspath(mod.__file__))


def enabled_names(environ) -> list[str]:
    names = environ.get("BACKENDS", "").split()
    if not names:
        raise DeployError("BACKENDS is empty: list the backends to run in .env, e.g. BACKENDS=\"browser_clear browser_tor\"")
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise DeployError(f"BACKENDS lists {', '.join(dup)} twice")
    return names


def load_backends(names: list[str], eps=None) -> list[Backend]:
    eps = list(entry_points() if eps is None else eps)
    by_name = {}
    for ep in eps:
        by_name.setdefault(ep.name, ep)
    out = []
    for n in names:
        if not NAME_RE.match(n):
            raise DeployError(f"{n!r} is not a backend name (lower-case words joined by underscores, e.g. browser_tor)")
        if n not in by_name:
            raise DeployError(f"backend {n!r} is not installed (installed: {', '.join(sorted(by_name)) or 'none'}); "
                              f"for an out-of-tree backend add its pip requirement to BACKEND_PACKAGES in .env")
        ep = by_name[n]
        out.append(Backend(n, ep.load(), package_dir(ep)))
    return out


# ---------------------------------------------------------------- validation --------------------------------------
def check_descriptor(b: Backend) -> list[str]:
    errs = []
    d = b.d
    for m in MEMBERS:
        if not hasattr(d, m):
            errs.append(f"{b.name}: descriptor has no `{m}`")
    if errs:
        return errs
    if d.name != b.ep_name:
        errs.append(f"{b.name}: descriptor name {d.name!r} differs from its entry-point name {b.ep_name!r}")
    if not (isinstance(d.title, str) and d.title.strip() and isinstance(d.description, str) and d.description.strip()):
        errs.append(f"{b.name}: title and description must be non-empty text (they are shown to the model)")
    if ":" not in d.runtime:
        errs.append(f"{b.name}: runtime must be a `module:Object` import string")
    if not os.path.isfile(b.path(d.compose)):
        errs.append(f"{b.name}: compose overlay {b.path(d.compose)} does not exist")
    for tag, ctx in d.builds:
        if not os.path.isfile(os.path.join(b.path(ctx), "Dockerfile")):
            errs.append(f"{b.name}: build {tag}: no Dockerfile in {b.path(ctx)}")
    for hook in OPTIONAL[:2]:
        if b.optional(hook) and not os.path.isfile(b.path(b.optional(hook))):
            errs.append(f"{b.name}: {hook} script {b.path(b.optional(hook))} does not exist")
    if b.optional("env") is not None and not callable(b.optional("env")):
        errs.append(f"{b.name}: env must be a function of the environment")
    return errs


def _env_keys(env) -> set:
    if isinstance(env, dict):
        return set(env)
    return {str(e).split("=", 1)[0] for e in (env or [])}


def _env_value(env, key):
    if isinstance(env, dict):
        return env.get(key)
    for e in env or []:
        k, _, v = str(e).partition("=")
        if k == key:
            return v
    return None


def check_overlay(b: Backend, overlay: dict) -> list[str]:
    """The overlay contract: every service, network and volume prefixed with the hyphenated name; the gateway is
    `<h>-gateway` with BACKEND, BACKENDS and BACKEND_SERVERS in its environment; the base nginx service may only be given
    networks (this backend's own)."""
    errs, p = [], b.h + "-"
    services = overlay.get("services") or {}
    for s, body in services.items():
        if s == "nginx":
            extra = set(body or {}) - {"networks"}
            if extra:
                errs.append(f"{b.name}: the overlay may only add `networks` to the base nginx service, not {', '.join(sorted(extra))}")
            for n in body.get("networks") or []:
                n = n if isinstance(n, str) else n
                if not (n.startswith(p) or n in SHARED_NETWORKS):
                    errs.append(f"{b.name}: nginx joins network {n!r}, which is not {p}*")
        elif not s.startswith(p):
            errs.append(f"{b.name}: service {s!r} must be prefixed {p!r}")
    for kind in ("networks", "volumes"):
        for n in overlay.get(kind) or {}:
            if not n.startswith(p):
                errs.append(f"{b.name}: {kind[:-1]} {n!r} must be prefixed {p!r}")
    gw = services.get(f"{p}gateway")
    if gw is None:
        errs.append(f"{b.name}: the overlay has no service {p}gateway")
    else:
        env = gw.get("environment")
        for k in ("BACKEND", "BACKENDS", "BACKEND_SERVERS"):
            if k not in _env_keys(env):
                errs.append(f"{b.name}: {p}gateway has no {k} in its environment")
        if _env_value(env, "BACKEND") not in (None, b.name):
            errs.append(f"{b.name}: {p}gateway sets BACKEND={_env_value(env, 'BACKEND')}, expected {b.name}")
    for s in getattr(b.d, "services", []):
        if s not in services:
            errs.append(f"{b.name}: descriptor lists service {s!r}, which the overlay does not define")
    return errs


def load_overlay(b: Backend) -> dict:
    import yaml  # PyYAML: installed into .venv by deploy.sh
    with open(b.path(b.d.compose)) as f:
        return yaml.safe_load(f) or {}


def validate(backends: list[Backend]) -> None:
    errs = []
    for b in backends:
        e = check_descriptor(b)
        errs += e
        if not e:
            errs += check_overlay(b, load_overlay(b))
    if errs:
        raise DeployError("\n".join(errs))


# ---------------------------------------------------------------- actions -----------------------------------------
def hook_env(b: Backend, environ) -> dict:
    return {**environ, "BACKEND": b.name, "BACKEND_HYPHEN": b.h}


def run_hook(b: Backend, member: str, environ, run=subprocess.run) -> None:
    script = b.optional(member)
    if not script:
        return
    if member == "preflight":
        r = run(["sh", b.path(script)], env=hook_env(b, environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            raise DeployError(f"{b.name}: preflight failed:\n{(r.stderr or r.stdout).strip()}")
    else:
        r = run(["sh", b.path(script)], env=hook_env(b, environ))
        if r.returncode != 0:
            raise DeployError(f"{b.name}: {member} failed (exit {r.returncode})")


def build_plan(backends: list[Backend]) -> list[tuple[str, str, str]]:
    """(tag, context dir, owner) in order, deduped by tag, the first descriptor to name a tag wins."""
    plan, seen = [], set()
    for b in backends:
        for tag, ctx in b.d.builds:
            if tag not in seen:
                seen.add(tag)
                plan.append((tag, b.path(ctx), b.name))
    return plan


def dockerfile_args(ctx: str) -> list[str]:
    with open(os.path.join(ctx, "Dockerfile")) as f:
        return re.findall(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)", f.read(), re.M)


def build_command(tag: str, ctx: str, wheels: str, environ) -> list[str]:
    """docker build with the wheels as the named context `wheels` (Dockerfiles `COPY --from=wheels`), and every ARG the
    Dockerfile declares fed from the environment variable of the same name when it is set."""
    cmd = ["docker", "build", "--build-context", f"wheels={wheels}", "-t", tag]
    for a in dockerfile_args(ctx):
        if environ.get(a):
            cmd += ["--build-arg", f"{a}={environ[a]}"]
    return cmd + [ctx]


def build(backends: list[Backend], wheels: str, environ, run=subprocess.run) -> None:
    for tag, ctx, owner in build_plan(backends):
        print(f"==> building {tag} ({owner})", file=sys.stderr, flush=True)
        if run(build_command(tag, ctx, wheels, environ), env=environ).returncode != 0:
            raise DeployError(f"docker build of {tag} failed")


def backend_servers(backends: list[Backend]) -> str:
    return json.dumps([{"name": b.name, "title": b.d.title, "description": b.d.description} for b in backends])


def derived_env(backends: list[Backend], environ) -> dict:
    out = {"BACKENDS": " ".join(b.name for b in backends), "BACKEND_SERVERS": backend_servers(backends)}
    for b in backends:
        fn = b.optional("env")
        if fn:
            res = fn(dict(environ))
            if not isinstance(res, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in res.items()):
                raise DeployError(f"{b.name}: env() must return a dict of str to str")
            out.update(res)
    return out


def landing_page(backends: list[Backend], scheme: str, host: str) -> str:
    root = f"{scheme}://{host}"
    cmds = "\n".join(f"claude mcp add --transport http spider-hoarder-{b.h} {root}/{b.h}/mcp" for b in backends)
    items = "".join(f"<li><b>{html.escape(b.h)}</b> ({html.escape(b.d.title)}): {html.escape(b.d.description)}</li>" for b in backends)
    return ("<!doctype html><html><head><meta charset=utf-8><title>spider-hoarder</title></head><body>"
            "<h1>spider-hoarder</h1><p>Add the MCP servers to Claude Code:</p>"
            f"<pre>{html.escape(cmds)}</pre>"
            "<p>Then run <code>/mcp</code>, pick the server and choose Authenticate.</p>"
            f"<ul>{items}</ul></body></html>\n")


def dev_overlay(backends: list[Backend]) -> str:
    """Dev-only settings of the gateways (`localhost` is the host, so the local Keycloak's issuer URL works inside them)."""
    out = 'version: "3.8"\nservices:\n'
    for b in backends:
        out += (f"  {b.h}-gateway:\n    extra_hosts: [\"localhost:host-gateway\"]\n"
                f"    entrypoint: [\"tini\", \"--\", \"/localhost-is-host.sh\", \"/app/entrypoint.sh\"]\n"
                f"    volumes: [\"./dev/localhost-is-host.sh:/localhost-is-host.sh:ro\"]\n")
    return out


# ---------------------------------------------------------------- cli ---------------------------------------------
def main(argv=None, environ=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    try:
        backends = load_backends(enabled_names(environ))
        if cmd == "validate":
            validate(backends)
        elif cmd == "preflight":
            for b in backends:
                run_hook(b, "preflight", environ)
        elif cmd == "build":
            build(backends, args[0], environ)
        elif cmd == "post-build":
            for b in backends:
                run_hook(b, "post_build", environ)
        elif cmd == "env":
            for k, v in derived_env(backends, environ).items():
                print(f"export {k}={shlex.quote(v)}")
        elif cmd == "landing":
            os.makedirs(os.path.dirname(os.path.abspath(args[0])), exist_ok=True)
            with open(args[0], "w") as f:
                f.write(landing_page(backends, environ.get("PUBLIC_SCHEME", "https"), environ["PUBLIC_HOST"]))
        elif cmd == "compose":
            for b in backends:
                print("-c")
                print(b.path(b.d.compose))
        elif cmd == "services":
            seen = []
            for b in backends:
                seen += [s for s in b.d.services if s not in seen]
            print("\n".join(seen))
        elif cmd == "dev-overlay":
            os.makedirs(os.path.dirname(os.path.abspath(args[0])), exist_ok=True)
            with open(args[0], "w") as f:
                f.write(dev_overlay(backends))
        else:
            print(f"unknown command {cmd!r}\n\n{__doc__}", file=sys.stderr)
            return 2
    except DeployError as e:
        print(f"deploy: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
