#!/usr/bin/env python3
"""Deploy tooling of spider-hoarder, run by deploy.sh inside the virtualenv .venv/ (standalone: not part of any package).

Backends register a descriptor under the entry-point group `spider_hoarder.backends`; $BACKENDS (space-separated canonical
names, e.g. `browser_clear browser_tor android_tor`) says which are enabled. Everything here reads the installed
descriptors with importlib.metadata and uses PyYAML for the overlay check; see docs/architecture/backend-plugins.md.

  validate           every name in BACKENDS is installed, descriptors are well formed, overlays obey the contract
  preflight          run each backend's preflight script (non-zero = abort, with its stderr)
  build              docker build -f <Dockerfile> <repo root> (and push, when IMAGE_PREFIX is set) every image of the enabled backends, once per tag
  build-image TAG CTX  the same for one image outside the descriptors (deploy.sh's oauth2-proxy-dcr)
  pin FILE           placement overlay: base oauth2-proxy and every enabled backend's `pinned` services on $PIN_DATA
                     (writes nothing when PIN_DATA is empty)
  post-build         run each backend's post_build script
  env                `export K=V` lines: every backend's env(environ) result, BACKENDS and BACKEND_SERVERS
  landing FILE       the static landing page (claude mcp add lines + each backend's title and description)
  compose            one `-c <overlay>` argument per line
  services           the services to force-restart after deploy, one per line (unprefixed)
  dev-overlay FILE   stack overlay with the dev-only settings of every enabled gateway
  seaweedfs-config DIR  the stack's SeaweedFS's s3.json and iam.json, generated from the SEAWEEDFS_* variables
  seaweedfs-overlay FILE  stack overlay joining every enabled gateway to the SeaweedFS network
  parse-env          `export K=V` lines for the parser service: PARSE_BLOB_OPTIONS, PARSE_OCR_TABLE, PARSE_PROXY_ALLOW
  parse-overlay FILE stack overlay wiring the parser service to every enabled backend (and the OCR sidecar, when OCR_PROFILES is set)
  build-parse        docker build (and push) of the parser, blob-sidecar and (with OCR_PROFILES) ocr-sidecar images
  parse-services     the parser service's services to force-restart after deploy, one per line

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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the build context of every backend image
GROUP = "spider_hoarder.backends"
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")
MEMBERS = ("name", "title", "description", "compose", "builds", "services", "runtime")  # required; the rest are optional
OPTIONAL = ("preflight", "post_build", "env")  # and `pinned`: services with node-local state, a subset of `services`
BASE_PINNED = ("oauth2-proxy",)  # the base stack file's services with local state
SHARED_NETWORKS = ("public", "auth")  # the base stack file's networks, usable by overlays


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
        errs.append(f"{b.name}: stack overlay {b.path(d.compose)} does not exist")
    for tag, dockerfile in d.builds:
        if not os.path.isfile(b.path(dockerfile)):
            errs.append(f"{b.name}: build {tag}: Dockerfile {b.path(dockerfile)} does not exist")
    for hook in OPTIONAL[:2]:
        if b.optional(hook) and not os.path.isfile(b.path(b.optional(hook))):
            errs.append(f"{b.name}: {hook} script {b.path(b.optional(hook))} does not exist")
    extra = set(b.optional("pinned") or []) - set(d.services)
    if extra:
        errs.append(f"{b.name}: pinned lists {', '.join(sorted(extra))}, which are not in services")
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


def seaweedfs_config(environ) -> tuple[dict, dict]:
    """(s3.json, iam.json) of the stack's SeaweedFS: one identity, the gateways', with the Admin action (creating a role per job needs
    IAM rights), and the STS block with default-deny policy. Roles live in SeaweedFS's memory, so a restart ends the credentials
    in flight (the consumer re-mints). 900 s is the shortest lifetime STS accepts."""
    import base64
    need = ("SEAWEEDFS_KEY", "SEAWEEDFS_SECRET", "SEAWEEDFS_SIGNING_KEY")
    bad = [v for v in need if environ.get(v, "") in ("", "changeme", "change-me")]
    if bad:
        raise DeployError(f"{', '.join(bad)} unset or still a placeholder (set them in .env)")
    try:
        ok = len(base64.b64decode(environ["SEAWEEDFS_SIGNING_KEY"], validate=True)) >= 32
    except ValueError:
        ok = False
    if not ok:
        raise DeployError("SEAWEEDFS_SIGNING_KEY must be base64 of at least 32 random bytes (openssl rand -base64 32)")
    s3 = {"identities": [{"name": "gateway", "credentials": [{"accessKey": environ["SEAWEEDFS_KEY"], "secretKey": environ["SEAWEEDFS_SECRET"]}],
                          "actions": ["Admin", "Read", "Write", "List", "Tagging"]}]}
    iam = {"sts": {"tokenDuration": "15m", "maxSessionLength": "1h", "issuer": "spider-hoarder-sts", "signingKey": environ["SEAWEEDFS_SIGNING_KEY"]},
           "policy": {"defaultEffect": "Deny", "storeType": "memory"}}
    return s3, iam


PARSE_TOKENS = ("WRAPPER_TOKEN", "PROXY_TOKEN")
PLACEHOLDERS = ("", "changeme", "change-me")


def ocr_profiles(environ) -> dict:
    """OCR_PROFILES: {name: {token, base_url, model, auth?, ...}} (JSON, the sidecar's table); {} when unset."""
    raw = (environ.get("OCR_PROFILES") or "").strip()
    try:
        profiles = json.loads(raw) if raw else {}
    except ValueError as e:
        raise DeployError(f"OCR_PROFILES is not JSON: {e}")
    if not isinstance(profiles, dict):
        raise DeployError("OCR_PROFILES must be a JSON object: {name: {token, base_url, model, auth}}")
    return profiles


def check_parse_env(backends: list[Backend], environ) -> None:
    """The parser service's secrets and OCR settings: both tokens set, not placeholders and different secrets; every OCR profile
    complete with its own token; a backend's `<NAME>_PARSE_OCR` names a profile that exists."""
    errs = [f"{v} is unset or still a placeholder (set it in .env)" for v in PARSE_TOKENS if environ.get(v, "") in PLACEHOLDERS]
    if not errs and environ["WRAPPER_TOKEN"] == environ["PROXY_TOKEN"]:
        errs.append("WRAPPER_TOKEN and PROXY_TOKEN must be different secrets")
    profiles = ocr_profiles(environ)
    for name, p in profiles.items():
        if not isinstance(p, dict) or not all(isinstance(p.get(k), str) and p[k] for k in ("token", "base_url", "model")):
            errs.append(f"OCR_PROFILES[{name}] needs token, base_url and model")
        elif p.get("auth", "none") not in ("none", "bearer", "google"):
            errs.append(f"OCR_PROFILES[{name}].auth must be none, bearer or google")
        elif p.get("auth") == "bearer" and not p.get("api_key"):
            errs.append(f"OCR_PROFILES[{name}] has auth bearer but no api_key")
        elif p.get("auth") == "google" and not p.get("audience"):
            errs.append(f"OCR_PROFILES[{name}] has auth google but no audience")
    tokens = [p.get("token") for p in profiles.values() if isinstance(p, dict)]
    if len(set(tokens)) != len(tokens) or {*tokens} & {environ.get(v) for v in PARSE_TOKENS}:
        errs.append("OCR_PROFILES tokens must be unique and differ from WRAPPER_TOKEN and PROXY_TOKEN (the token picks the profile)")
    for b in backends:
        want = environ.get(f"{b.name.upper()}_PARSE_OCR", "")
        if want and want not in profiles:
            errs.append(f"{b.name.upper()}_PARSE_OCR={want} is not a profile of OCR_PROFILES")
    if errs:
        raise DeployError("\n".join(errs))


def parse_env(environ) -> dict:
    """What the parser service's stack file reads: the store's options without credentials (the job's scoped credential comes with each
    request), the wrapper's OCR table (name -> sidecar URL + token) and the proxy exception for the stack's own SeaweedFS."""
    opts = json.loads(environ.get("RESEARCH_BLOB_OPTIONS") or "{}")
    return {"PARSE_BLOB_OPTIONS": json.dumps({k: v for k, v in opts.items() if k not in ("key", "secret", "token", "anon")}),
            "PARSE_OCR_TABLE": json.dumps({n: {"url": "http://ocr-sidecar:8080", "token": p["token"]} for n, p in ocr_profiles(environ).items()}),
            "PARSE_PROXY_ALLOW": "seaweedfs:8333" if environ.get("SEAWEEDFS") == "1" else environ.get("PARSE_PROXY_ALLOW", "")}


def parse_overlay(backends: list[Backend], environ) -> str:
    """Per enabled backend a network `<h>-parse` that only its gateway and the shared parser are on (one shared network would let the gateways
    reach each other), PARSE_URL / PARSE_TOKEN / PARSE_OCR in the gateway; the proxy on the SeaweedFS network when that is the store; the OCR
    sidecar when profiles are configured."""
    import yaml
    doc = {"version": "3.8", "services": {"parser": {"networks": [f"{b.h}-parse" for b in backends]}}, "networks": {}}
    for b in backends:
        doc["services"][f"{b.h}-gateway"] = {"networks": [f"{b.h}-parse"], "environment": {
            "PARSE_URL": "http://parser:8080", "PARSE_TOKEN": "${WRAPPER_TOKEN}", "PARSE_OCR": f"${{{b.name.upper()}_PARSE_OCR:-}}"}}
        doc["networks"][f"{b.h}-parse"] = {"driver": "overlay", "internal": True}
    if environ.get("SEAWEEDFS") == "1":
        doc["services"]["blob-sidecar"] = {"networks": ["seaweedfs"]}
    if ocr_profiles(environ):
        doc["services"]["ocr-sidecar"] = {
            "image": "${IMAGE_PREFIX:-}spider-hoarder-ocr-sidecar:${IMAGE_TAG:-local}", "cap_drop": ["ALL"],
            "cap_add": ["NET_ADMIN", "SETUID", "SETGID", "SETPCAP", "KILL"], "deploy": {"replicas": 1, "restart_policy": {"condition": "any"}},
            "environment": {"OCR_PROFILES": "${OCR_PROFILES}", "OCR_TIMEOUT": "${OCR_TIMEOUT:-120}", "OCR_MAX_BODY": "${OCR_MAX_BODY:-10485760}",
                            "OCR_MAX_CONCURRENT": "${OCR_MAX_CONCURRENT:-4}"}, "networks": ["parse-ocr", "public"]}
    return yaml.safe_dump(doc, sort_keys=False)


def parse_services(environ) -> list[str]:
    return ["parser", "blob-sidecar", *(["ocr-sidecar"] if ocr_profiles(environ) else [])]


def build_parse(environ, run=subprocess.run) -> None:
    for tag, dockerfile, context in (("spider-hoarder-parser:local", "parser/Dockerfile", REPO_ROOT),
                                     ("spider-hoarder-blob-sidecar:local", "blob-sidecar/Dockerfile", os.path.join(REPO_ROOT, "blob-sidecar")),
                                     *([("spider-hoarder-ocr-sidecar:local", "ocr-sidecar/Dockerfile", os.path.join(REPO_ROOT, "ocr-sidecar"))]
                                       if ocr_profiles(environ) else [])):
        build_one(tag, os.path.join(REPO_ROOT, dockerfile), context, environ, "parse", run)


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
    """(tag, Dockerfile, owner) in order, deduped by tag, the first descriptor to name a tag wins."""
    plan, seen = [], set()
    for b in backends:
        for tag, dockerfile in b.d.builds:
            if tag not in seen:
                seen.add(tag)
                plan.append((tag, b.path(dockerfile), b.name))
    return plan


def dockerfile_args(dockerfile: str) -> list[str]:
    with open(dockerfile) as f:
        return re.findall(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)", f.read(), re.M)


def image_ref(tag: str, environ) -> str:
    """The real image name of a descriptor tag `<name>:local`: `${IMAGE_PREFIX}<name>:${IMAGE_TAG}` (what the stack files say)."""
    return f"{environ.get('IMAGE_PREFIX') or ''}{tag.removesuffix(':local')}:{environ.get('IMAGE_TAG') or 'local'}"


def build_command(tag: str, dockerfile: str, context: str, environ) -> list[str]:
    """Plain `docker build -f <dockerfile> <context>`, and every ARG the Dockerfile declares fed from the environment
    variable of the same name when it is set."""
    cmd = ["docker", "build", "-f", dockerfile, "-t", image_ref(tag, environ)]
    for a in dockerfile_args(dockerfile):
        if environ.get(a):
            cmd += ["--build-arg", f"{a}={environ[a]}"]
    return cmd + [context]


def build_one(tag: str, dockerfile: str, context: str, environ, owner="deploy", run=subprocess.run) -> None:
    ref = image_ref(tag, environ)
    print(f"==> building {ref} ({owner})", file=sys.stderr, flush=True)
    if run(build_command(tag, dockerfile, context, environ), env=environ).returncode != 0:
        raise DeployError(f"docker build of {ref} failed")
    if environ.get("IMAGE_PREFIX") and run(["docker", "push", ref], env=environ).returncode != 0:
        raise DeployError(f"docker push of {ref} failed")


def build(backends: list[Backend], environ, run=subprocess.run) -> None:
    for tag, dockerfile, owner in build_plan(backends):
        build_one(tag, dockerfile, REPO_ROOT, environ, owner, run)


def pin_overlay(backends: list[Backend], environ) -> str:
    """Placement constraint $PIN_DATA on the services with node-local state; empty string (no overlay) when PIN_DATA is unset."""
    constraint = (environ.get("PIN_DATA") or "").strip()
    if not constraint:
        return ""
    names = list(BASE_PINNED)
    for b in backends:
        names += [s for s in b.optional("pinned") or [] if s not in names]
    return 'version: "3.8"\nservices:\n' + "".join(
        f"  {s}:\n    deploy:\n      placement:\n        constraints: [{json.dumps(constraint)}]\n" for s in names)


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


def seaweedfs_overlay(backends: list[Backend]) -> str:
    """Every enabled gateway joins the network of the stack's SeaweedFS (docker-compose.seaweedfs.yml)."""
    return 'version: "3.8"\nservices:\n' + "".join(f"  {b.h}-gateway:\n    networks: [seaweedfs]\n" for b in backends)


# ---------------------------------------------------------------- cli ---------------------------------------------
def main(argv=None, environ=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "seaweedfs-config":  # needs no backend
            os.makedirs(args[0], exist_ok=True)
            for name, doc in zip(("s3.json", "iam.json"), seaweedfs_config(environ)):
                with open(os.path.join(args[0], name), "w") as f:
                    json.dump(doc, f, indent=1)
            return 0
        backends = load_backends(enabled_names(environ))
        if cmd == "validate":
            validate(backends)
            check_parse_env(backends, environ)
        elif cmd == "preflight":
            for b in backends:
                run_hook(b, "preflight", environ)
        elif cmd == "build":
            build(backends, environ)
        elif cmd == "build-image":
            build_one(args[0], os.path.join(args[1], "Dockerfile"), args[1], environ)
        elif cmd == "pin":
            overlay = pin_overlay(backends, environ)
            if overlay:
                os.makedirs(os.path.dirname(os.path.abspath(args[0])), exist_ok=True)
                with open(args[0], "w") as f:
                    f.write(overlay)
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
        elif cmd == "seaweedfs-overlay":
            os.makedirs(os.path.dirname(os.path.abspath(args[0])), exist_ok=True)
            with open(args[0], "w") as f:
                f.write(seaweedfs_overlay(backends))
        elif cmd == "parse-env":
            for k, v in parse_env(environ).items():
                print(f"export {k}={shlex.quote(v)}")
        elif cmd == "parse-overlay":
            os.makedirs(os.path.dirname(os.path.abspath(args[0])), exist_ok=True)
            with open(args[0], "w") as f:
                f.write(parse_overlay(backends, environ))
        elif cmd == "build-parse":
            build_parse(environ)
        elif cmd == "parse-services":
            print("\n".join(parse_services(environ)))
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
