"""Descriptor of browser_clear: plain data and stdlib only (it is loaded by deploy.sh in the deploy host's virtualenv, which has no
gateway dependencies). File paths are relative to this package; family build contexts are found with importlib.resources.
See docs/architecture/backend-plugins.md."""

from importlib import resources
from types import SimpleNamespace


def _family(sub):
    return str(resources.files("spider_hoarder_browser_sdk") / sub)


descriptor = SimpleNamespace(
    name="browser_clear",
    title='Clear-text browser',
    description=(
        "Isolated browser sessions that reach the public internet directly (this server's own IP, not anonymised). Fast "
        'and reliable: the default for ordinary public websites, JS-heavy pages, consent walls and anything that needs '
        'a real browser.'),
    compose="docker-compose.yml",
    builds=[
        ("spider-hoarder-browser-base:local", _family("image")),
        ("spider-hoarder-browser-clear:local", "image"),
    ],
    services=['browser-clear-gateway'],
    preflight=None,
    post_build=None,
    env=None,
    runtime="browser_clear.runtime:driver",
)
