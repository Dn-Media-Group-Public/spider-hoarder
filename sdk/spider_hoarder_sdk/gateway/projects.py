"""Projects and roles. `projects/<proj>/access.json` ({name, created, users: {<email>: admin|write|read}}) is the authority;
`users/<user>/<proj>` is only an index for "my projects" (a stale entry grants nothing). Only the gateway reads or writes
either. Also the per-MCP-session state of which project job-creating tools write to. See docs/architecture/auth.md."""

import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from spider_hoarder_sdk.content import storage

ROLES = {"read": 1, "write": 2, "admin": 3}
CACHE_TTL = float(os.environ.get("ACCESS_CACHE_SECONDS", 30))
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class NotFound(ValueError):
    """The one answer for a project that does not exist and one the caller has no role in."""

    def __init__(self):
        super().__init__("project not found")


def enc_user(email: str) -> str:
    """Lowercased email as one path component: everything outside [a-z0-9._@+-] (so also `/`, `%`, `~`) becomes %XX. An email
    always contains `@`, so the result is never `.` or `..`."""
    email = email.strip().lower()
    if "@" not in email or len(email) > 254:
        raise ValueError("invalid email")
    return re.sub(r"[^a-z0-9._@+-]", lambda m: "".join(f"%{b:02X}" for b in m.group().encode()), email)


def _access_key(proj: str) -> str:
    return f"projects/{proj}/access.json"


_cache: dict[str, tuple[float, dict | None]] = {}


def _load(proj: str) -> dict | None:
    if not UUID_RE.match(proj or ""):
        return None
    hit = _cache.get(proj)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    raw = storage.read_object(_access_key(proj))
    data = json.loads(raw) if raw is not None else None
    _cache[proj] = (time.time(), data)
    return data


def exists(proj: str) -> bool:
    return _load(proj) is not None


def _save(proj: str, data: dict) -> None:
    storage.write_object(_access_key(proj), json.dumps(data).encode())
    _cache[proj] = (time.time(), data)


def require(owner: str, proj: str, need: str = "read") -> dict:
    """The project's access data if `owner` has at least role `need`, else NotFound (no project and no role look the same)."""
    data = _load(proj)
    role = data and data["users"].get(owner.lower())
    if not role:
        raise NotFound()
    if ROLES[role] < ROLES[need]:
        raise PermissionError(f"this needs the {need} role in the project (you have {role})")
    return data


def create(owner: str, name: str) -> str:
    proj = str(uuid.uuid4())
    _save(proj, {"name": str(name)[:200], "created": time.time(), "users": {owner.lower(): "admin"}})
    storage.write_object(f"users/{enc_user(owner)}/{proj}", b"admin")
    return proj


def list_for(owner: str) -> list[dict]:
    """Projects the caller is a member of: the index entries, each confirmed against access.json."""
    projs = [p for p in storage.list_names(f"users/{enc_user(owner)}") if UUID_RE.match(p)]
    with ThreadPoolExecutor(8) as ex:
        datas = list(ex.map(_load, projs))
    return [{"project": p, "name": d["name"], "role": d["users"][owner.lower()]}
            for p, d in zip(projs, datas) if d and owner.lower() in d["users"]]


def get(owner: str, proj: str) -> dict:
    data = require(owner, proj)
    role = data["users"][owner.lower()]
    return {"project": proj, "name": data["name"], "created": data["created"], "role": role,
            **({"users": data["users"]} if role == "admin" else {})}


def _admins(data: dict) -> int:
    return sum(1 for r in data["users"].values() if r == "admin")


def share(owner: str, proj: str, email: str, role: str) -> None:
    """Add a user or change their role (admin only). Grant order: access.json, then the index entry."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    idx = enc_user(email)
    data = json.loads(json.dumps(require(owner, proj, "admin")))  # copy: the cached dict is only replaced by _save
    email = email.strip().lower()
    if data["users"].get(email) == "admin" and role != "admin" and _admins(data) == 1:
        raise ValueError("a project must keep at least one admin")
    data["users"][email] = role
    _save(proj, data)
    storage.write_object(f"users/{idx}/{proj}", role.encode())


def unshare(owner: str, proj: str, email: str) -> None:
    """Remove a user (admin only). Revoke order: access.json, then the index entry."""
    idx = enc_user(email)
    data = json.loads(json.dumps(require(owner, proj, "admin")))
    email = email.strip().lower()
    if email not in data["users"]:
        raise ValueError("that user is not in the project")
    if data["users"][email] == "admin" and _admins(data) == 1:
        raise ValueError("a project must keep at least one admin")
    del data["users"][email]
    _save(proj, data)
    storage.delete_object(f"users/{idx}/{proj}")


def delete(owner: str, proj: str) -> list[str]:
    """Delete the project (admin only): access.json first (nothing can be authorised or minted after it), then the
    members' index entries, then the whole prefix. ponytail: credentials minted before the first step can still write until
    they expire (orphans nobody can reach); no second pass, add a lifecycle rule or sweeper if that matters."""
    data = require(owner, proj, "admin")
    storage.delete_object(_access_key(proj))
    _cache.pop(proj, None)
    for email in data["users"]:
        storage.delete_object(f"users/{enc_user(email)}/{proj}")
    sids = forget_project(proj)
    storage.delete_tree(f"projects/{proj}")
    return sids  # the MCP sessions that were bound to it: the caller ends them


# ---- per-MCP-session state: which project job-creating tools write to, and the <session> directory they write under
bound: dict[str, str] = {}         # MCP session id -> project (open_session); dropped when the driver session ends
job_sessions: dict[str, str] = {}  # MCP session id -> <session> uuid, created at its first job; dropped with the MCP session


def bind(sid: str | None, proj: str) -> None:
    bound[sid or ""] = proj


def unbind(sid: str | None) -> None:
    bound.pop(sid or "", None)


def forget(sid: str | None) -> None:
    unbind(sid)
    job_sessions.pop(sid or "", None)


def forget_project(proj: str) -> list[str]:
    """Unbind every MCP session bound to `proj`; returns their ids (the caller ends those sessions)."""
    sids = [s for s, p in bound.items() if p == proj]
    for s in sids:
        bound.pop(s)
    return sids


def resolve(sid: str | None, project: str | None = None) -> str:
    """The project a job-creating call writes to: the explicit `project`, else the one the MCP session is bound to."""
    if project:
        if not UUID_RE.match(project):
            raise NotFound()
        return project
    if (sid or "") in bound:
        return bound[sid or ""]
    raise ValueError("no project: pass `project`, or bind this session with open_session(project) first")


def session_dir(sid: str | None) -> str:
    return job_sessions.setdefault(sid or "", str(uuid.uuid4()))
