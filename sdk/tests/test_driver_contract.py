"""Driver contract, checked against every backend installed in the venv. Each runs in its own subprocess: the gateway
chooses its driver at import."""

import importlib.metadata
import json
import os
import subprocess
import sys

import pytest

NAMES = sorted(e.name for e in importlib.metadata.entry_points(group="spider_hoarder.backends")
               if not e.name.startswith("fake_"))

CHECK = r'''
import asyncio, inspect, json, os
from spider_hoarder_sdk.gateway import app, sessions
from spider_hoarder_sdk.gateway.driver import Driver, hyphen
from spider_hoarder_sdk.gateway.pool import Pool

d, name = app.driver, os.environ["BACKEND"]
assert isinstance(d, Driver)
assert d.name == name and d.prefix == hyphen(name) == app.PREFIX and "_" not in d.prefix
assert d.noun in ("browser", "device")
assert 0 < d.idle_timeout <= 86400 and 0 < d.help_timeout and d.max_per_user >= 1 and d.close_timeout >= 30
assert d.max_total is None or d.max_total >= 1
assert d.instructions.strip()
names = [t.name for t in d.extra_tools]
job = {t.name for t in app.JOB_TOOLS}
assert len(names) == len(set(names)) and not set(names) & job, names
assert isinstance(d.hidden, (set, frozenset)) and isinstance(d.file_tools, dict) and all(isinstance(v, str) for v in d.file_tools.values())
assert set(d.capped_tools) <= set(d.file_tools)
pool = d.make_pool()
assert isinstance(pool, Pool) and pool.all and pool.free == pool.all
assert inspect.iscoroutinefunction(d.upstream_tools)
if d.noun == "device":
    tools = asyncio.run(d.upstream_tools())
    assert tools and all(t.name for t in tools) and set(d.file_tools) <= {t.name for t in tools}
    assert d.known(tools[0].name) and not d.known("no_such_tool")
others = app.other_servers()
assert name not in others and hyphen(name) not in others.replace("spider-hoarder-" + hyphen(name), "")
assert "spider-hoarder-other-one (Other One): It does things." in others.splitlines()
assert "spider-hoarder-no-text" in others.splitlines()
assert app.mcp.name == "spider-hoarder-" + d.prefix
assert app.mcp.instructions.startswith(d.instructions) and app.mcp.instructions.endswith(others)
print("ok")
'''


@pytest.mark.parametrize("name", NAMES)
def test_driver_contract(name):
    servers = [{"name": "other_one", "title": "Other One", "description": "It does things."},
               {"name": name, "title": "Self", "description": "must not be listed"}]
    env = {**os.environ, "BACKEND": name, "BACKENDS": f"{name} other_one no_text",
           "BACKEND_SERVERS": json.dumps(servers), "BLOB_TOKEN_SECRET": "x", "PUBLIC_HOST": "h.example",
           "OIDC_ISSUER": "https://kc.example/realms/r", "REPLICA_RID": "10-0-0-5", "REPLICA_SLOT": "1",
           "MAX_REPLICAS": "4", "ANDROID_SLOTS_PER_REPLICA": "2", "SLOT_SECRET": "s"}
    r = subprocess.run([sys.executable, "-c", CHECK], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-2000:]


def test_all_backends_are_covered():
    assert {"browser_clear", "browser_tor", "browser_i2p", "android_clear", "android_tor"} <= set(NAMES)
