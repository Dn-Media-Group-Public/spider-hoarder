"""Projects: roles, the access.json authority vs the users/ index, last admin, email encoding, delete, the project tools through the
gateway, links without an email, and the scoped-credential policies."""

import asyncio
import json
import tempfile
import uuid

import pytest

from spider_hoarder_sdk import testing

testing.gateway_env()

from spider_hoarder_sdk.content import storage
from spider_hoarder_sdk.gateway import app, blobtoken, jobs, projects


@pytest.fixture(autouse=True)
def store(monkeypatch):
    testing.fresh_store()
    monkeypatch.setattr(projects, "CACHE_TTL", 0)  # every check reads the store
    projects._cache.clear(); projects.bound.clear(); projects.job_sessions.clear()


def test_email_encoding_is_injective_and_one_component():
    enc = projects.enc_user
    assert enc("A@X.y") == "a@x.y" and enc("a+b@x.y") == "a+b@x.y"
    assert enc("a/b@x.y") == "a%2Fb@x.y" and enc("a%2Fb@x.y") == "a%252fb@x.y" and enc("a~b@x.y") == "a%7Eb@x.y"
    assert len({enc(e) for e in ("a/b@x.y", "a_b@x.y", "a%2Fb@x.y", "a b@x.y")}) == 4  # the old sanitiser mapped these together
    for bad in ("", "..", ".", "nope"):
        with pytest.raises(ValueError):
            enc(bad)


def test_role_matrix_and_identical_not_found():
    p = projects.create("adm@x.y", "P")
    projects.share("adm@x.y", p, "Rd@x.y", "read")
    projects.share("adm@x.y", p, "wr@x.y", "write")
    for user, need, ok in (("rd@x.y", "read", 1), ("rd@x.y", "write", 0), ("wr@x.y", "write", 1), ("wr@x.y", "admin", 0), ("adm@x.y", "admin", 1)):
        if ok:
            projects.require(user, p, need)
        else:
            with pytest.raises(PermissionError):
                projects.require(user, p, need)
    msgs = set()
    for user, proj in (("nobody@x.y", p), ("adm@x.y", str(uuid.uuid4())), ("adm@x.y", "../x"), ("adm@x.y", "")):
        with pytest.raises(projects.NotFound) as e:
            projects.require(user, proj)
        msgs.add(str(e.value))
    assert msgs == {"project not found"}
    assert "users" not in projects.get("rd@x.y", p) and projects.get("adm@x.y", p)["users"]["rd@x.y"] == "read"  # members only for admins
    with pytest.raises(PermissionError):
        projects.share("wr@x.y", p, "z@x.y", "read")


def test_my_projects_trusts_access_json_not_the_index():
    p, q = projects.create("a@x.y", "P"), projects.create("b@x.y", "Q")
    projects.share("a@x.y", p, "c@x.y", "read")
    storage.write_object(f"users/c@x.y/{q}", b"admin")  # a stale/forged index entry grants nothing
    assert [(x["project"], x["role"]) for x in projects.list_for("c@x.y")] == [(p, "read")]
    projects.unshare("a@x.y", p, "c@x.y")
    assert projects.list_for("c@x.y") == [] and storage.list_names("users/c@x.y") == [q]
    with pytest.raises(projects.NotFound):
        projects.require("c@x.y", q)


def test_concurrent_grants_keep_a_valid_access_json():
    """access.json is a read-modify-write of one object (no conditional writes in fsspec): two admins saving at once can lose one
    grant, which is accepted; the file must stay valid and the creator an admin."""
    from concurrent.futures import ThreadPoolExecutor
    p = projects.create("a@x.y", "P")
    emails = [f"u{i}@x.y" for i in range(8)]
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda e: projects.share("a@x.y", p, e, "read"), emails))
    data = json.loads(storage.read_object(f"projects/{p}/access.json"))
    assert data["users"]["a@x.y"] == "admin" and 1 <= sum(e in data["users"] for e in emails) <= 8


def test_last_admin_stays():
    p = projects.create("a@x.y", "P")
    for f in (lambda: projects.unshare("a@x.y", p, "a@x.y"), lambda: projects.share("a@x.y", p, "a@x.y", "write")):
        with pytest.raises(ValueError, match="at least one admin"):
            f()
    projects.share("a@x.y", p, "b@x.y", "admin")
    projects.unshare("a@x.y", p, "a@x.y")
    assert projects.require("b@x.y", p, "admin")


def test_new_job_needs_write_and_job_ids_are_project_scoped():
    p = projects.create("a@x.y", "P")
    projects.share("a@x.y", p, "r@x.y", "read")
    jid, key = jobs._new_job("a@x.y", "s1", p)
    assert jid.startswith(p + "/") and key == f"projects/{jid.replace('/', '/data/', 1)}"
    assert jobs._new_job("a@x.y", "s1", p)[0].split("/")[1] == jid.split("/")[1]  # one <session> per MCP session
    assert jobs._key("r@x.y", jid) == key
    with pytest.raises(PermissionError):
        jobs._new_job("r@x.y", "s1", p)
    with pytest.raises(projects.NotFound):
        jobs._key("o@x.y", jid)
    with pytest.raises(ValueError, match="no project"):
        jobs._new_job("a@x.y", "unbound")
    projects.bind("s2", p)
    assert jobs._new_job("a@x.y", "s2")[0].startswith(p)


def test_project_tools_and_delete_ends_bound_sessions(monkeypatch):
    call = lambda who, tool, **a: (setattr(app, "_caller", lambda: (None, who, "sid1")), asyncio.run(app.call_tool(tool, a)))[1]
    out = lambda r: json.loads(r.content[0].text)
    p = out(call("a@x.y", "create_project", name="P"))["project"]
    assert out(call("a@x.y", "list_projects")) == [{"project": p, "name": "P", "role": "admin"}]
    assert out(call("a@x.y", "share_project", project=p, email="b@x.y", role="write")) == {"ok": True}
    assert out(call("b@x.y", "get_project", project=p))["role"] == "write"
    assert call("b@x.y", "share_project", project=p, email="c@x.y", role="read").isError
    r = call("c@x.y", "get_project", project=p)
    assert r.isError and "project not found" in r.content[0].text
    ended = []

    async def end(sid):
        ended.append(sid)
    monkeypatch.setattr(app.sessions, "end_mcp_session", end)
    projects.bind("sidB", p)
    assert call("b@x.y", "delete_project", project=p).isError and projects.bound == {"sidB": p}  # not an admin
    assert out(call("a@x.y", "delete_project", project=p)) == {"ok": True}
    assert ended == ["sidB"] and not projects.bound
    assert not projects.exists(p) and storage.list_names("users/b@x.y") == []
    fs, path = storage._abs(f"projects/{p}")
    assert not [k for k, i in fs.find(path, detail=True).items() if i["type"] == "file"]  # SeaweedFS keeps an empty directory entry: files only


def test_delete_tree_reports_leftovers(monkeypatch):
    storage.write_object("projects/p/data/s/j/x", b"1")
    fs, _ = storage._fs()
    monkeypatch.setattr(fs, "rm", lambda *a, **k: None)  # a delete that silently failed (s3fs drops per-key Errors); s3fs binds rm on the instance
    with pytest.raises(OSError, match="left 1"):
        storage.delete_tree("projects/p")


def test_links_carry_no_email_and_upload_writes_only_its_job():
    p = projects.create("a@x.y", "P")
    app._caller = lambda: (None, "a@x.y", "sid1")
    r = asyncio.run(app.call_tool("get_upload_link", {"project": p}))
    out = json.loads(r.content[0].text)
    token = out["url"].rsplit("/", 1)[1]
    assert "/upload/" in out["url"] and "x.y" not in blobtoken._unb64(token.split(".")[0]).decode()
    assert app._upload_key(token) == jobs.job_key(out["job_id"])
    projects.delete("a@x.y", p)
    with pytest.raises(Exception, match="not found"):
        app._upload_key(token)  # a deleted project takes its upload links with it
    # a read-only member cannot mint one
    q = projects.create("a@x.y", "Q"); projects.share("a@x.y", q, "r@x.y", "read")
    app._caller = lambda: (None, "r@x.y", "s")
    assert asyncio.run(app.call_tool("get_upload_link", {"project": q})).isError
    # file links: the token names <proj>/<session>/<job>/path
    jid, key = testing.new_job("a@x.y", q)
    storage.write_bytes(key, "original/a.txt", b"hi", "text/plain")
    app._caller = lambda: (None, "r@x.y", "s")
    url = json.loads(asyncio.run(app.call_tool("get_file_url", {"job_id": jid, "path": "original/a.txt"})).content[0].text)["url"]
    assert blobtoken.verify(url.rsplit("/", 1)[1]) == {"job_id": jid, "path": "original/a.txt"} and "/blob/" in url and app.RID not in url
