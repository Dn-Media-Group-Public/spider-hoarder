"""Pure checks of the no-media policy (no browser needed)."""

import pytest

from spider_hoarder_browser_sdk.mediapolicy import Policy, TEXT_ALLOWED, blocked_message


def test_defaults_per_egress():
    assert Policy.make(True).media and not Policy.make(False).media and not Policy.make(False).media
    assert Policy.make(False, media=True).media


def test_text_allowlist_and_media_blocked():
    p = Policy.make(False)
    for ok in ("text/html; charset=utf-8", "TEXT/plain", "application/json", "application/ld+json", "application/atom+xml",
               "application/xhtml+xml", "application/xml"):
        assert not p.content_type_blocked(ok), ok
    for bad in ("image/png", "image/svg+xml", "video/mp4", "audio/mpeg", "application/pdf", "application/octet-stream",
                "application/x-shockwave-flash", "", None, "application/jsonx"):
        assert p.content_type_blocked(bad), bad


def test_precedence_deny_allow_default():
    p = Policy.make(False, allow=["image/svg+xml", "image/*"], deny=["image/gif", "text/geojson"])
    assert not p.content_type_blocked("image/svg+xml; charset=x") and not p.content_type_blocked("image/png")
    assert p.content_type_blocked("image/gif") and p.content_type_blocked("text/geojson")  # deny beats allow and default
    assert not p.content_type_blocked("text/html")


def test_deny_with_media_true():
    p = Policy.make(True, deny=["text/geojson"])
    assert p.active and p.content_type_blocked("text/geojson; x=y") and not p.content_type_blocked("image/png")
    assert not p.resource_blocked("image")
    assert not Policy.make(True).active


def test_resource_types():
    p = Policy.make(False)
    assert all(p.resource_blocked(t) for t in ("image", "media", "object", "imageset", "font"))
    assert not any(p.resource_blocked(t) for t in ("document", "script", "stylesheet", "xhr", "fetch"))


def test_validation():
    for kw in ({"media": "no"}, {"allow": "image/png"}, {"deny": ["png"]}, {"allow": [1]}):
        with pytest.raises(ValueError):
            Policy.make(False, **kw)


def test_summary_and_roundtrip():
    s = Policy.make(False, allow=["Image/SVG+xml"], deny=["text/csv"]).summary("tor", True)
    assert s == {"egress": "tor", "opened": True, "media": False,
                 "blocked_resource_types": ["image", "media", "object", "imageset", "font"],
                 "allowed_content_types": TEXT_ALLOWED + ["image/svg+xml"], "denied_content_types": ["text/csv"]}
    p = Policy.make(False, allow=["image/png"])
    assert Policy.from_json(p.to_json()) == p
    assert Policy.make(True).summary("clear", False)["blocked_resource_types"] == []
    assert "image/png" in blocked_message("image/png; x")
