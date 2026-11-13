"""Container start of the ocr-sidecar (as root, under tini): the pinned egress rules (NET_ADMIN), then the forwarder as `ocr` with no
capabilities. Needs OCR_PROFILES (JSON, see app.py)."""

import json
import os
import pwd
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
import egress_rules  # noqa: E402


def main() -> None:
    ocr = pwd.getpwnam("ocr")
    ns = [p[1] for p in (l.split() for l in open("/etc/resolv.conf")) if len(p) > 1 and p[0] == "nameserver" and p[1].replace(".", "").isdigit()]
    eps = egress_rules.endpoints(json.loads(os.environ["OCR_PROFILES"]), lambda h: socket.getaddrinfo(h, None, socket.AF_INET)[0][4][0])
    for cmd in egress_rules.rules(ocr.pw_uid, ns, eps):
        subprocess.run(cmd, check=True)
    os.execvp("setpriv", ["setpriv", "--no-new-privs", f"--reuid={ocr.pw_uid}", f"--regid={ocr.pw_gid}", "--clear-groups",
                          "--bounding-set=-all", "--inh-caps=-all", "uvicorn", "app:app_from_env", "--factory", "--app-dir", "/app",
                          "--host", "0.0.0.0", "--port", os.environ.get("OCR_PORT", "8080")])


if __name__ == "__main__":
    main()
