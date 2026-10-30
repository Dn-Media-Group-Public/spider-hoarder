"""Shared test fixtures: the gateway environment and a fake backend registered through real entry-point discovery."""

import os
import sys
import textwrap

import mcp.types as types

from spider_hoarder_sdk.gateway.pool import Pool

ENV_DEFAULTS = dict(BLOB_TOKEN_SECRET="test-secret", PUBLIC_HOST="h.example", OIDC_ISSUER="https://kc.example/realms/r",
                    REPLICA_RID="10-0-0-5")


def gateway_env(**over) -> None:
    """Environment the gateway needs at import: defaults are only set when missing, keyword arguments always."""
    for k, v in ENV_DEFAULTS.items():
        os.environ.setdefault(k, v)
    for k, v in over.items():
        os.environ[k] = str(v)


class FakeBackend:
    """pool.Pool backend that records its calls."""

    def __init__(self):
        self.calls = []

    async def start(self, uid, env, files):
        self.calls.append(("start", uid))

    async def stop(self, uid):
        self.calls.append(("stop", uid))

    async def reset(self):
        self.calls.append(("reset",))


def _fake_driver_class():
    from spider_hoarder_sdk.gateway.driver import Driver

    class FakeDriver(Driver):
        noun = "fake"
        max_per_user = 1
        instructions = "A fake backend."

        def __init__(self, uids=range(1, 4)):
            self.backend = FakeBackend()
            self.uids = uids

        def make_pool(self):
            return Pool(self.backend, uids=self.uids)

        def upstream_mcp(self, uid):
            return f"http://127.0.0.1:1/{uid}", {}, {}

        def view_upstream(self, uid, rid):
            return f"127.0.0.1:{6000 + uid}"

        async def upstream_tools(self):
            return [types.Tool(name="fake_tool", inputSchema={"type": "object", "properties": {}}, description="fake")]

    return FakeDriver


def __getattr__(name):  # FakeDriver imports driver.py, which loads $BACKEND: only on demand
    if name == "FakeDriver":
        return _fake_driver_class()
    raise AttributeError(name)


DEFAULT_RUNTIME = '''\
from spider_hoarder_sdk.testing import FakeDriver

driver = FakeDriver()
'''


def install_fake_backend(directory, name="fake_backend", runtime_src=None, title="Fake", description="A fake backend") -> str:
    """Write module `name` and a dist-info registering it under spider_hoarder.backends into `directory`, put that on sys.path."""
    directory = str(directory)
    pkg = os.path.join(directory, name)
    os.makedirs(pkg, exist_ok=True)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    with open(os.path.join(pkg, "descriptor.py"), "w") as f:
        f.write(textwrap.dedent(f'''\
            from types import SimpleNamespace

            descriptor = SimpleNamespace(
                name={name!r}, title={title!r}, description={description!r},
                compose="docker-compose.yml", builds=[], services=[{name.replace("_", "-") + "-gateway"!r}],
                runtime="{name}.runtime:driver")
            '''))
    with open(os.path.join(pkg, "runtime.py"), "w") as f:
        f.write(runtime_src or DEFAULT_RUNTIME)
    dist = os.path.join(directory, f"{name}-0.0.0.dist-info")
    os.makedirs(dist, exist_ok=True)
    with open(os.path.join(dist, "METADATA"), "w") as f:
        f.write(f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.0.0\n")
    with open(os.path.join(dist, "entry_points.txt"), "w") as f:
        f.write(f"[spider_hoarder.backends]\n{name} = {name}.descriptor:descriptor\n")
    if directory not in sys.path:
        sys.path.insert(0, directory)
    return name
