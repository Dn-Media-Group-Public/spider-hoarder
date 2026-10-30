"""scripts/spider_hoarder.py against fake entry points and throwaway descriptor packages (and the real in-tree descriptors)."""

import json
import os
import stat
import subprocess
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import spider_hoarder as sh  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

GOOD_OVERLAY = """
services:
  nginx:
    networks: [dummy-one-sessions]
  dummy-one-gateway:
    image: x
    environment:
      BACKEND: dummy_one
      BACKENDS: ${BACKENDS}
      BACKEND_SERVERS: ${BACKEND_SERVERS}
    networks: [dummy-one-sessions, public]
  dummy-one-helper:
    image: y
networks:
  dummy-one-sessions: {driver: overlay}
volumes:
  dummy-one-data: {}
"""


class FakeEP:
    def __init__(self, name, desc, value=None):
        self.name, self._d = name, desc
        self.value = value or f"{name}.descriptor:descriptor"

    def load(self):
        return self._d


def make_pkg(tmp_path, name="dummy_one", overlay=GOOD_OVERLAY, builds=(("img-a:local", "ctx_a"),), dockerfile="FROM scratch\n", **over):
    """A package dir with a Dockerfile per build context and an overlay; returns (descriptor, Backend, EP)."""
    pkg = tmp_path / name
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "docker-compose.yml").write_text(overlay)
    for _, ctx in builds:
        (pkg / ctx).mkdir(exist_ok=True)
        (pkg / ctx / "Dockerfile").write_text(dockerfile)
    d = types.SimpleNamespace(name=name, title="Dummy", description="Does dummy things", compose="docker-compose.yml",
                              builds=list(builds), services=["dummy-one-gateway"], runtime=f"{name}.runtime:driver", **over)
    return d, sh.Backend(name, d, str(pkg)), FakeEP(name, d)


def script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path.name


# ---------------------------------------------------------------- BACKENDS ----------------------------------------
def test_empty_and_duplicate_backends():
    with pytest.raises(sh.DeployError, match="BACKENDS is empty"):
        sh.enabled_names({})
    with pytest.raises(sh.DeployError, match="browser_tor twice"):
        sh.enabled_names({"BACKENDS": "browser_tor browser_clear browser_tor"})
    assert sh.enabled_names({"BACKENDS": " a_b   c_d "}) == ["a_b", "c_d"]


def test_unknown_and_malformed_names(tmp_path):
    _, _, ep = make_pkg(tmp_path)
    with pytest.raises(sh.DeployError, match=r"not installed.*dummy_one.*BACKEND_PACKAGES"):
        sh.load_backends(["other_thing"], eps=[ep])
    for bad in ("Browser_Tor", "browser-tor", "nounderscore", "a b"):
        with pytest.raises(sh.DeployError, match="not a backend name"):
            sh.load_backends([bad], eps=[ep])


def test_load_backends_resolves_package_dir(tmp_path, monkeypatch):
    d, b, ep = make_pkg(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    (got,) = sh.load_backends(["dummy_one"], eps=[types.SimpleNamespace(name="dummy_one", value="dummy_one.descriptor:descriptor", load=lambda: d)])
    assert got.dir == str(tmp_path / "dummy_one") and got.h == "dummy-one" and got.d is d


# ---------------------------------------------------------------- descriptor shape --------------------------------
def test_descriptor_ok_and_shape_errors(tmp_path):
    d, b, _ = make_pkg(tmp_path)
    assert sh.check_descriptor(b) == []
    del d.title
    assert any("no `title`" in e for e in sh.check_descriptor(b))
    d.title = "T"
    d.name = "other_name"
    assert any("differs from its entry-point name" in e for e in sh.check_descriptor(b))
    d.name = "dummy_one"
    d.description, d.runtime, d.compose = "  ", "noimportstring", "missing.yml"
    errs = "\n".join(sh.check_descriptor(b))
    assert "non-empty text" in errs and "module:Object" in errs and "missing.yml" in errs
    d.description, d.runtime, d.compose = "x", "a:b", "docker-compose.yml"
    d.builds = [("t:local", "nodir")]
    assert any("no Dockerfile" in e for e in sh.check_descriptor(b))
    d.builds = []
    d.preflight, d.post_build, d.env = "nope.sh", "nope2.sh", "notcallable"
    errs = "\n".join(sh.check_descriptor(b))
    assert "preflight script" in errs and "post_build script" in errs and "env must be a function" in errs


# ---------------------------------------------------------------- overlay contract --------------------------------
def overlay_errs(tmp_path, overlay, **over):
    d, b, _ = make_pkg(tmp_path, overlay=overlay, **over)
    return sh.check_overlay(b, sh.load_overlay(b))


def test_clean_overlay_passes(tmp_path):
    assert overlay_errs(tmp_path, GOOD_OVERLAY) == []


@pytest.mark.parametrize("old,new,msg", [
    ("  dummy-one-helper:", "  helper:", "service 'helper' must be prefixed"),
    ("  dummy-one-sessions: {driver: overlay}", "  sessions: {driver: overlay}", "network 'sessions' must be prefixed"),
    ("  dummy-one-data: {}", "  data: {}", "volume 'data' must be prefixed"),
    ("    networks: [dummy-one-sessions]\n  dummy-one-gateway", "    networks: [elsewhere]\n  dummy-one-gateway", "nginx joins network 'elsewhere'"),
    ("    networks: [dummy-one-sessions]\n  dummy-one-gateway", "    ports: ['1:1']\n  dummy-one-gateway", "only add `networks`"),
    ("      BACKEND: dummy_one\n", "", "no BACKEND in its environment"),
    ("      BACKENDS: ${BACKENDS}\n", "", "no BACKENDS in its environment"),
    ("      BACKEND_SERVERS: ${BACKEND_SERVERS}\n", "", "no BACKEND_SERVERS in its environment"),
    ("BACKEND: dummy_one", "BACKEND: dummy_two", "sets BACKEND=dummy_two"),
    ("  dummy-one-gateway:", "  dummy-one-gatewayy:", "no service dummy-one-gateway"),
])
def test_overlay_violations(tmp_path, old, new, msg):
    assert old in GOOD_OVERLAY
    errs = overlay_errs(tmp_path, GOOD_OVERLAY.replace(old, new))
    assert any(msg in e for e in errs), errs


def test_overlay_environment_as_list_and_missing_descriptor_service(tmp_path):
    ov = GOOD_OVERLAY.replace("""    environment:
      BACKEND: dummy_one
      BACKENDS: ${BACKENDS}
      BACKEND_SERVERS: ${BACKEND_SERVERS}
""", """    environment: [BACKEND=dummy_one, BACKENDS=x, BACKEND_SERVERS=y]
""")
    assert overlay_errs(tmp_path, ov) == []
    d, b, _ = make_pkg(tmp_path, overlay=GOOD_OVERLAY)
    d.services = ["dummy-one-gateway", "dummy-one-ghost"]
    assert any("'dummy-one-ghost', which the overlay does not define" in e for e in sh.check_overlay(b, sh.load_overlay(b)))


def test_validate_reports_all_errors(tmp_path):
    _, b, _ = make_pkg(tmp_path, overlay=GOOD_OVERLAY.replace("dummy-one-helper:", "helper:"))
    with pytest.raises(sh.DeployError, match="helper"):
        sh.validate([b])
    (tmp_path / "x").mkdir()
    _, ok, _ = make_pkg(tmp_path / "x")
    sh.validate([ok])


# ---------------------------------------------------------------- builds ------------------------------------------
def test_build_plan_dedupes_by_tag_first_wins(tmp_path):
    _, a, _ = make_pkg(tmp_path, name="dummy_one", builds=(("base:local", "base"), ("a:local", "img")))
    _, b, _ = make_pkg(tmp_path, name="dummy_two", builds=(("base:local", "base"), ("b:local", "img")))
    plan = sh.build_plan([a, b])
    assert [(t, o) for t, _, o in plan] == [("base:local", "dummy_one"), ("a:local", "dummy_one"), ("b:local", "dummy_two")]
    assert plan[0][1] == str(tmp_path / "dummy_one" / "base")


def test_absolute_context_paths_win(tmp_path):
    _, a, _ = make_pkg(tmp_path)
    fam = tmp_path / "family"
    fam.mkdir()
    (fam / "Dockerfile").write_text("FROM scratch\n")
    a.d.builds = [("fam:local", str(fam))]
    assert sh.build_plan([a]) == [("fam:local", str(fam), "dummy_one")]


def test_build_command_feeds_only_declared_args_that_are_set(tmp_path):
    ctx = tmp_path / "c"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM x\nARG ANDROID_API=34\n  ARG NOT_A_DECL\nARG ANDROID_FDROID_URL\nARG UNSET_ONE\nRUN echo\n")
    cmd = sh.build_command("t:local", str(ctx), "/w", {"ANDROID_FDROID_URL": "u", "ANDROID_API": "", "OTHER": "z", "UNSET_ONE": ""})
    assert cmd == ["docker", "build", "--build-context", "wheels=/w", "-t", "t:local", "--build-arg", "ANDROID_FDROID_URL=u", str(ctx)]


def test_build_runs_each_tag_once_and_fails_loudly(tmp_path):
    _, a, _ = make_pkg(tmp_path, name="dummy_one", builds=(("base:local", "base"), ("a:local", "img")))
    _, b, _ = make_pkg(tmp_path, name="dummy_two", builds=(("base:local", "base"),))
    calls = []

    def run(cmd, env=None):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0)
    sh.build([a, b], "/w", {}, run=run)
    assert [c[c.index("-t") + 1] for c in calls] == ["base:local", "a:local"]
    with pytest.raises(sh.DeployError, match="docker build of base:local failed"):
        sh.build([a], "/w", {}, run=lambda cmd, env=None: types.SimpleNamespace(returncode=1))


# ---------------------------------------------------------------- hooks -------------------------------------------
def test_preflight_failure_aborts_with_stderr(tmp_path):
    d, b, _ = make_pkg(tmp_path)
    d.preflight = script(tmp_path / "dummy_one" / "pre.sh", 'echo "no kvm for $BACKEND" >&2; exit 3\n')
    with pytest.raises(sh.DeployError, match="preflight failed:\nno kvm for dummy_one"):
        sh.run_hook(b, "preflight", {"PATH": os.environ["PATH"]})
    d.preflight = script(tmp_path / "dummy_one" / "pre.sh", "exit 0\n")
    sh.run_hook(b, "preflight", {"PATH": os.environ["PATH"]})


def test_post_build_gets_backend_names_in_env(tmp_path):
    d, b, _ = make_pkg(tmp_path)
    out = tmp_path / "out"
    d.post_build = script(tmp_path / "dummy_one" / "post.sh", f'echo "$BACKEND $BACKEND_HYPHEN $STACK_NAME" > {out}\n')
    sh.run_hook(b, "post_build", {"PATH": os.environ["PATH"], "STACK_NAME": "st"})
    assert out.read_text().strip() == "dummy_one dummy-one st"
    d.post_build = script(tmp_path / "dummy_one" / "post.sh", "exit 7\n")
    with pytest.raises(sh.DeployError, match="post_build failed \\(exit 7\\)"):
        sh.run_hook(b, "post_build", {"PATH": os.environ["PATH"]})


def test_missing_hooks_are_skipped(tmp_path):
    _, b, _ = make_pkg(tmp_path)
    sh.run_hook(b, "preflight", {})
    sh.run_hook(b, "post_build", {})


# ---------------------------------------------------------------- env / landing / overlay files ------------------
def test_derived_env_merges_and_round_trips_servers(tmp_path):
    d1, a, _ = make_pkg(tmp_path, name="dummy_one", env=lambda e: {"DUMMY_ONE_SLOTS": str(int(e["R"]) * 2)})
    d2, b, _ = make_pkg(tmp_path, name="dummy_two")
    d2.title, d2.description = 'Two "quoted"', "d2"
    env = sh.derived_env([a, b], {"R": "3"})
    assert env["BACKENDS"] == "dummy_one dummy_two" and env["DUMMY_ONE_SLOTS"] == "6"
    assert json.loads(env["BACKEND_SERVERS"]) == [{"name": "dummy_one", "title": "Dummy", "description": "Does dummy things"},
                                                  {"name": "dummy_two", "title": 'Two "quoted"', "description": "d2"}]
    d1.env = lambda e: {"X": 1}
    with pytest.raises(sh.DeployError, match="str to str"):
        sh.derived_env([a], {})


def test_landing_page_lists_only_enabled_and_escapes(tmp_path):
    _, a, _ = make_pkg(tmp_path, name="dummy_one")
    a.d.title, a.d.description = "<b>T</b>", "a & b"
    page = sh.landing_page([a], "https", "h.example")
    assert "claude mcp add --transport http spider-hoarder-dummy-one https://h.example/dummy-one/mcp" in page
    assert "&lt;b&gt;T&lt;/b&gt;" in page and "a &amp; b" in page and "<b>T</b>" not in page
    assert "dummy-two" not in page


def test_dev_overlay(tmp_path):
    _, a, _ = make_pkg(tmp_path, name="dummy_one")
    _, b, _ = make_pkg(tmp_path, name="dummy_two")
    import yaml
    doc = yaml.safe_load(sh.dev_overlay([a, b]))
    assert set(doc["services"]) == {"dummy-one-gateway", "dummy-two-gateway"}
    gw = doc["services"]["dummy-one-gateway"]
    assert gw["extra_hosts"] == ["localhost:host-gateway"] and gw["entrypoint"][-2:] == ["/localhost-is-host.sh", "/app/entrypoint.sh"]


# ---------------------------------------------------------------- CLI ---------------------------------------------
@pytest.fixture
def cli(tmp_path, monkeypatch):
    d, b, ep = make_pkg(tmp_path, env=lambda e: {"DUMMY_ONE_SLOTS": "2"})
    d.services = ["dummy-one-gateway", "dummy-one-helper"]
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sh, "entry_points", lambda: [ep])
    monkeypatch.setattr(sh, "package_dir", lambda e: str(tmp_path / "dummy_one"))
    return {"BACKENDS": "dummy_one", "PUBLIC_HOST": "h.example", "PUBLIC_SCHEME": "http"}, tmp_path


def test_cli_commands(cli, capsys):
    env, tmp = cli
    assert sh.main(["validate"], env) == 0
    assert sh.main(["env"], env) == 0
    out = capsys.readouterr().out
    assert "export BACKENDS=dummy_one" in out and "export DUMMY_ONE_SLOTS=2" in out and "export BACKEND_SERVERS=" in out
    assert sh.main(["compose"], env) == 0
    assert capsys.readouterr().out.split("\n")[:2] == ["-c", str(tmp / "dummy_one" / "docker-compose.yml")]
    assert sh.main(["services"], env) == 0
    assert capsys.readouterr().out.split() == ["dummy-one-gateway", "dummy-one-helper"]
    assert sh.main(["landing", str(tmp / "b" / "landing.html")], env) == 0
    assert "http://h.example/dummy-one/mcp" in (tmp / "b" / "landing.html").read_text()
    assert sh.main(["dev-overlay", str(tmp / "b" / "dev.yml")], env) == 0
    assert "dummy-one-gateway" in (tmp / "b" / "dev.yml").read_text()


def test_cli_errors_exit_nonzero(cli, capsys):
    env, _ = cli
    assert sh.main(["validate"], {**env, "BACKENDS": "nothing_here"}) == 1
    assert "BACKEND_PACKAGES" in capsys.readouterr().err
    assert sh.main(["validate"], {**env, "BACKENDS": ""}) == 1
    assert sh.main(["bogus"], env) == 2
    assert sh.main([], env) == 0


# ---------------------------------------------------------------- the real backends -------------------------------
REAL = ["browser_clear", "browser_tor", "browser_i2p", "android_clear", "android_tor"]


def real_backends():
    names = {e.name for e in sh.entry_points()}
    if not set(REAL) <= names:
        pytest.skip("in-tree backends are not installed in this environment")
    return sh.load_backends(REAL)


def test_real_descriptors_validate():
    bs = real_backends()
    sh.validate(bs)
    plan = sh.build_plan(bs)
    tags = [t for t, _, _ in plan]
    assert len(tags) == len(set(tags)) and tags.count("spider-hoarder-browser-base:local") == 1
    assert tags.index("spider-hoarder-browser-base:local") < tags.index("spider-hoarder-browser-clear:local")
    assert {"spider-hoarder-android-slot:local", "spider-hoarder-android-wrapper:local"} <= set(tags)


def test_real_android_env_is_replicas_times_slots():
    bs = {b.name: b for b in real_backends()}
    env = sh.derived_env(list(bs.values()), {"ANDROID_TOR_REPLICAS": "3", "ANDROID_SLOTS_PER_REPLICA": "4"})
    assert env["ANDROID_TOR_SLOTS"] == "12" and env["ANDROID_CLEAR_SLOTS"] == "4"  # clear defaults to one replica
    assert sh.derived_env([bs["android_tor"]], {})["ANDROID_TOR_SLOTS"] == "2"
    assert "ANDROID_TOR_SLOTS" not in sh.derived_env([bs["browser_tor"]], {})


def test_real_preflight_scripts_reject_missing_secrets(tmp_path):
    bs = {b.name: b for b in real_backends()}
    env = {"PATH": os.environ["PATH"], "SLOT_SECRET": "changeme", "ANDROID_FDROID_URL": "u", "ANDROID_FDROID_SHA256": "s"}
    if not os.path.exists("/dev/kvm"):
        with pytest.raises(sh.DeployError, match="/dev/kvm"):
            sh.run_hook(bs["android_tor"], "preflight", env)
    else:
        with pytest.raises(sh.DeployError, match="SLOT_SECRET"):
            sh.run_hook(bs["android_tor"], "preflight", env)
