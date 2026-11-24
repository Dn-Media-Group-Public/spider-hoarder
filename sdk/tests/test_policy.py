"""Policy levels: bounds, narrowing, ASK, stale values (docs/implemented-plans/policy-levels.md). Pure, no storage."""
import pytest

from spider_hoarder_sdk.gateway import policy as P

PROF = {"ocr": ["lan", "remote"], "asr": ["w", "w2"], "caption": ["v"]}


def res(admin=None, project=None, session=None, **kw):
    return P.resolve([("admin", {**P.ADMIN_DEFAULT, **(admin or {})}), ("project", project or {}), ("session", session or {})], PROF, **kw)


def test_defaults_keep_todays_behaviour():
    r = res()
    assert r["media"]["value"] == P.ASK and r["media"]["bound"] is None  # the level below chooses
    assert [r[f]["value"] for f in P.FEATURES] == [[], [], []] and r["ocr"]["bound"] == []  # features: off, and nothing to choose
    assert P.with_defaults(r, {"media": False, "allow": [], "deny": []})["media"] == {**r["media"], "value": False, "source": "default"}
    assert P.check("allow", ["image/svg+xml"], P.bound_of(r, "allow"), PROF) == ["image/svg+xml"]  # unconfigured: allow is not closed
    with pytest.raises(ValueError):
        P.check("ocr", ["lan"], P.bound_of(r, "ocr"), PROF)  # a session cannot add a feature


def test_ask_is_outside_the_order_and_bound_is_nearest_non_ask():
    r = res(admin={"asr": ["w", "w2"]}, project={"asr": P.ASK})
    assert r["asr"]["value"] == P.ASK and r["asr"]["bound"] == ["w", "w2"] and r["asr"]["bound_source"] == "admin"  # ASK over a value: still bounded
    assert res(admin={"ocr": P.ASK})["ocr"]["bound"] is None  # all ASK: the whole domain
    r = res(admin={"asr": P.ASK}, project={"asr": ["w", "w2"]}, session={"asr": ["w2"]})
    assert r["asr"]["value"] == ["w2"] and r["asr"]["source"] == "session" and r["asr"]["bound_source"] == "session"
    assert P.unresolved(res(admin={"caption": P.ASK})) == {"media": None, "allow": None, "deny": None, "caption": None}


def test_narrowing_rules():
    b = lambda v: (v, "project")
    assert P.check("media", False, b(True), PROF) is False and P.check("media", True, b(None), PROF) is True
    with pytest.raises(ValueError):
        P.check("media", True, b(False), PROF)
    assert P.check("deny", ["a/b", "c/d"], b(["a/b"]), PROF) and pytest.raises(ValueError, P.check, "deny", [], b(["a/b"]), PROF)
    assert P.check("allow", ["image/png"], b(["image/png", "image/gif"]), PROF)
    with pytest.raises(ValueError):
        P.check("allow", ["image/jpeg"], b(["image/png"]), PROF)
    assert P.check("asr", ["w2", "w"], b(["w", "w2"]), PROF) == ["w2", "w"]  # reorder is narrowing
    assert P.check("asr", ["w"], b(["w", "w2"]), PROF) == ["w"] and P.check("asr", [], b(["w"]), PROF) == []
    with pytest.raises(ValueError):
        P.check("asr", ["w2"], b(["w"]), PROF)  # never add


def test_domains_and_shapes():
    for s, v in (("media", "yes"), ("allow", ["nonsense"]), ("deny", "image/png"), ("ocr", ["lan", "remote"]), ("asr", ["w", "w"]), ("asr", ["w", "w2", "x"]),
                 ("caption", ["nope"]), ("nope", [])):
        with pytest.raises(ValueError):
            P.normalize(s, v, PROF)
    assert P.normalize("asr", ["w", "w2"], PROF) == ["w", "w2"] and P.normalize("deny", ["Image/PNG; x=y"]) == ["image/png"]
    assert P.normalize("caption", ["anything"], None) == ["anything"]  # another backend's section: shape only


def test_stale_values_are_dropped_at_resolution():
    r = res(admin={"ocr": ["lan"]}, project={"ocr": ["remote"]})  # the operator tightened the ceiling after the project stored this
    assert r["ocr"]["value"] == ["lan"] and r["ocr"]["source"] == "admin"
    r = res(admin={"asr": P.ASK}, project={"asr": ["gone"]})  # a profile that no longer exists
    assert r["asr"]["value"] == P.ASK
    r = res(admin={"media": False}, project={"media": True})
    assert r["media"]["value"] is False
    # a dropped project value is no bound for the session
    r = res(admin={"asr": P.ASK}, project={"asr": ["gone"]}, session={"asr": ["w"]})
    assert r["asr"]["value"] == ["w"]


def test_media_suggestion_is_clipped_to_the_bound():
    r = P.with_defaults(res(admin={"media": P.ASK}, project={"media": P.ASK}), {"media": True})
    assert r["media"]["value"] is True
    r = res(admin={"media": False}, project={"media": P.ASK})
    assert P.with_defaults(r, {"media": True})["media"]["value"] is False  # explicit False stays (not ASK, so untouched)
    r = {"media": {"value": P.ASK, "source": "project", "bound": False, "bound_source": "admin"}}
    assert P.with_defaults(r, {"media": True})["media"]["value"] is False  # ASK over false: clipped, not an error


def test_admin_env():
    env = {"POLICY_MEDIA": "false", "POLICY_ALLOW": "image/svg+xml, text/csv", "POLICY_ASR": "w,w2", "POLICY_CAPTION": "ASK", "POLICY_OCR": ""}
    a = P.admin_level(env, PROF)
    assert a == {"media": False, "allow": ["image/svg+xml", "text/csv"], "deny": P.ASK, "ocr": [], "asr": ["w", "w2"], "caption": P.ASK}
    for bad in ({"POLICY_MEDIA": "maybe"}, {"POLICY_OCR": "nope"}, {"POLICY_ASR": "w,w2,w"}, {"POLICY_DENY": "x"}):
        with pytest.raises(ValueError, match="POLICY_"):
            P.admin_level(bad, PROF)
    assert P.profile_names({"PROFILE_NAMES_OCR": "a,b"}) == {"ocr": ["a", "b"], "asr": [], "caption": []}


def test_features_and_notes():
    r = res(admin={"asr": ["w", "w2"], "caption": ["v"]}, project={"caption": []})
    assert P.features(P.chains(r)) == {"asr": "w", "asr-fallback": "w2"} and P.features({"ocr": []}) is None
    assert P.notes(r, PROF) == ["ocr: off (admin policy)", "asr: w,w2 (admin)", "caption: off (project policy)"]
    assert P.notes(res(admin={"ocr": P.ASK}), PROF)[0] == "ocr: off (unresolved)" and P.notes(r, {}) == []
    v = P.view(res(admin={"ocr": P.ASK}))
    assert v["ask"]["ocr"] == "any" and "ocr" not in v["policy"]
