"""Descriptor of android_clear: plain data and stdlib only (it is loaded by deploy.sh in the deploy host's virtualenv, which has no
gateway dependencies). File paths are relative to this package; family files are found with importlib.resources.
See docs/architecture/backend-plugins.md."""

from importlib import resources
from types import SimpleNamespace


def _family(sub):
    return str(resources.files("spider_hoarder_android_sdk") / sub)


def _env(environ):
    """Slot total of this backend: gateway replicas x slots per replica (the wrapper makes that many slots)."""
    replicas = int(environ.get("ANDROID_CLEAR_REPLICAS") or 1)
    return {"ANDROID_CLEAR_SLOTS": str(replicas * int(environ.get("ANDROID_SLOTS_PER_REPLICA") or 2))}


descriptor = SimpleNamespace(
    name="android_clear",
    title='Android emulator (direct)',
    description=(
        'A real Android emulator (Google Play image), one private device per conversation, wiped when the session ends; '
        "its traffic leaves from this server's public IP, not anonymised. For driving apps through accessibility-tree "
        'tools (tap, type, swipe, screenshots, APK install).'),
    compose="docker-compose.yml",
    builds=[
        ("spider-hoarder-android-base:local", _family("image")),
        ("spider-hoarder-android-slot:local", _family("slot")),   # ARGs of its Dockerfile are fed from the environment
        ("spider-hoarder-android-wrapper:local", _family("wrapper")),
        ("spider-hoarder-android-clear:local", "image"),
    ],
    services=['android-clear-gateway', 'android-clear-wrapper'],
    preflight=_family("preflight.sh"),
    post_build=_family("post_build.sh"),
    env=_env,
    runtime="android_clear.runtime:driver",
)
