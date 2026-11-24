"""Policy levels through the gateway: the admin env, a project's policy (create / get / update), open_session's arguments, what a job and an upload
link inherit (docs/implemented-plans/policy-levels.md). The pure rules are in test_policy.py."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from spider_hoarder_sdk import testing

testing.gateway_env()

from spider_hoarder_sdk.gateway import app, blobtoken, jobs, policy as lv, projects

ME = "fake_backend"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    testing.fresh_store()
    monkeypatch.setattr(projects, "CACHE_TTL", 0)
    projects._cache.clear(); projects.bound.clear(); projects.session_policy.clear(); projects.job_sessions.clear(); jobs.job_policy.clear()
    monkeypatch.setenv("PROFILE_NAMES_ASR", "w,w2")
    monkeypatch.setenv("PROFILE_NAMES_OCR", "lan")
    monkeypatch.setenv("POLICY_ASR", "ASK")
    app._caller = lambda: (None, "a@x.y", "sid1")


def call(tool, **args):
    r = asyncio.run(app.call_tool(tool, args))
    return r.isError, r.content[0].text


def ok(tool, **args):
    err, text = call(tool, **args)
    assert not err, text
    return json.loads(text)


def test_project_policy_is_validated_against_the_admin_level_and_shown_with_bounds(monkeypatch):
    p = ok("create_project", name="P", policy={ME: {"asr": ["w"], "media": False}, "other_backend": {"caption": ["anything"]}})["project"]
    got = ok("get_project", project=p)
    assert got["policy"] == {ME: {"asr": ["w"], "media": False}, "other_backend": {"caption": ["anything"]}}
    assert got["bounds"]["asr"] == {"bound": ["w"], "source": "project"} and got["bounds"]["media"] == {"bound": False, "source": "project"}
    assert got["bounds"]["ocr"] == {"bound": [], "source": "admin"} and got["bounds"]["deny"] == {"bound": "any", "source": "domain"}
    for bad in ({ME: {"asr": ["nope"]}}, {ME: {"ocr": ["lan"]}}, {ME: {"zzz": 1}}, {ME: {"media": "x"}}, {"Bad Name": {}}, ["list"], {ME: {"allow": ["x"]}}):
        assert call("create_project", name="Q", policy=bad)[0], bad
    monkeypatch.setenv("POLICY_ASR", "w")  # the operator tightens the ceiling: this project's wider value is refused on update ...
    assert call("update_project", project=p, policy={ME: {"asr": ["w", "w2"]}})[0]
    ok("update_project", project=p, policy={ME: {"asr": ["w"]}})  # ... and replaced wholesale otherwise
    assert ok("get_project", project=p)["policy"] == {ME: {"asr": ["w"]}}
    # admin role only
    projects.share("a@x.y", p, "w@x.y", "write")
    app._caller = lambda: (None, "w@x.y", "sid2")
    assert call("update_project", project=p, policy={})[0]
    assert ok("get_project", project=p)["policy"] == {ME: {"asr": ["w"]}}  # members read it


def test_a_stored_value_outside_a_tightened_ceiling_is_dropped_not_rewritten(monkeypatch):
    monkeypatch.setenv("POLICY_ASR", "w,w2")
    p = ok("create_project", name="P", policy={ME: {"asr": ["w", "w2"]}})["project"]
    monkeypatch.setenv("POLICY_ASR", "w")
    assert jobs.effective(p)["asr"]["value"] == ["w"] and jobs.effective(p)["asr"]["source"] == "admin"
    assert projects.get("a@x.y", p)["policy"] == {ME: {"asr": ["w", "w2"]}}


def test_open_session_refuses_while_a_setting_is_ask_and_bounds_the_choice():
    p = ok("create_project", name="P", policy={ME: {"asr": ["w", "w2"]}})["project"]
    assert lv.chains(jobs.check_session_policy("a@x.y", p, {}, lv.FEATURES, {})[1])["asr"] == ["w", "w2"]  # the project fixed it: nothing to ask
    p2 = ok("create_project", name="P2", policy={ME: {"asr": lv.ASK}})["project"]
    with pytest.raises(ValueError, match="needs a choice for: asr"):
        jobs.check_session_policy("a@x.y", p2, {}, lv.FEATURES, {})
    for bad in (["nope"], ["w", "w", "w2"], "w", lv.ASK):
        with pytest.raises(ValueError):
            jobs.check_session_policy("a@x.y", p2, {"asr": bad}, lv.FEATURES, {})
    level, res = jobs.check_session_policy("a@x.y", p2, {"asr": ["w2"]}, lv.FEATURES, {})
    assert level == {"asr": ["w2"]} and res["asr"]["source"] == "session" and lv.chains(res) == {"ocr": [], "asr": ["w2"], "caption": []}
    with pytest.raises(Exception):
        jobs.check_session_policy("nobody@x.y", p2, {"asr": ["w"]}, lv.FEATURES, {})  # needs the write role in the project


def test_a_job_and_an_upload_link_inherit_the_sessions_chains(monkeypatch):
    p = ok("create_project", name="P", policy={ME: {"asr": lv.ASK}})["project"]
    level, _ = jobs.check_session_policy("a@x.y", p, {"asr": ["w", "w2"]}, lv.FEATURES, {})
    projects.bind("sid1", p); projects.session_policy["sid1"] = level
    jid, key = jobs._new_job("a@x.y", "sid1")
    assert jobs.job_policy[key]["asr"] == {"value": ["w", "w2"], "source": "session", "bound": ["w", "w2"], "bound_source": "session"}
    link = ok("get_upload_link")["url"]
    token = link.rsplit("/", 1)[1]
    assert blobtoken.verify(token)["policy"] == {"ocr": [], "asr": ["w", "w2"], "caption": []} and len(jobs.job_policy) == 1  # the link's job entry is not kept
    # ending the session (unbind) drops the choices
    projects.unbind("sid1")
    assert "sid1" not in projects.session_policy
    # the page shows the chain read-only except for lowering; the server only ever takes a subset in the token's order
    page = asyncio.run(app.upload_page(SimpleNamespace(path_params={"token": token}))).body.decode()
    assert 'name="asr" value="w2" checked' in page and 'name="lowered"' in page and 'name="ocr"' not in page
    class Form(dict):
        def getlist(self, k): return [] if k not in self else self[k] if isinstance(self[k], list) else [self[k]]
    chains = {"asr": ["w", "w2"], "ocr": []}
    assert app._lowered(Form(), chains) == chains  # no choice sent: as signed
    assert app._lowered(Form(lowered="1", asr=["w2"]), chains) == {"asr": ["w2"], "ocr": []}
    assert app._lowered(Form(lowered="1", asr=["w2", "w", "evil"], ocr=["x"]), chains) == {"asr": ["w", "w2"], "ocr": []}  # never add, order of the link
    assert app._lowered(Form(lowered="1"), chains) == {"asr": [], "ocr": []}  # all unticked: off


def test_an_old_upload_link_without_a_policy_is_unresolved(monkeypatch):
    monkeypatch.setenv("POLICY_OCR", "lan")  # even with the admin level on
    p = ok("create_project", name="P")["project"]
    jid, key = testing.new_job("a@x.y", p)
    token = blobtoken.mint(jid, jobs.UPLOAD_MARK)
    assert app._upload_key(token)[1] == {}
    seen = []
    monkeypatch.setattr(jobs.parse_client, "_parse", lambda k, b, features: seen.append(features) or [])
    jobs._parse_job(key, "", None, {f: {"value": lv.ASK, "source": "unresolved"} for f in lv.FEATURES})
    assert seen == [None]
