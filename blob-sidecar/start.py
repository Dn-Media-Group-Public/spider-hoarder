"""Container start of the blob-sidecar (as root, under tini): the egress rules (NET_ADMIN), the proxy credentials, then squid as the
`proxy` user with no capabilities. Needs PROXY_TOKEN; PARSE_PROXY_ALLOW = space-separated host:port exceptions (dev blob store)."""

import os
import pwd
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
import egress_rules  # noqa: E402

CONF = """http_port 3128
max_filedescriptors 4096
pid_filename none
cache deny all
via off
forwarded_for delete
access_log stdio:/var/log/squid/access.log
cache_log /var/log/squid/cache.log
auth_param basic program /usr/lib/squid/basic_ncsa_auth {passwd}
auth_param basic realm parse
auth_param basic credentialsttl 5 minutes
acl authenticated proxy_auth REQUIRED
http_access allow authenticated
http_access deny all
"""
# CONNECT goes to any port: the default `deny CONNECT !SSL_ports` is not in this file, the firewall rules are the destination control


def resolve(host: str) -> str:
    return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]


def resolvers(path: str = "/etc/resolv.conf") -> list[str]:
    with open(path) as f:
        return [p[1] for p in (l.split() for l in f) if len(p) > 1 and p[0] == "nameserver" and p[1].replace(".", "").isdigit()]


def passwd_line(token: str) -> str:
    """htpasswd line for basic_ncsa_auth (apr1 MD5: squid 6 no longer accepts {SHA}); the token goes over stdin, not argv."""
    h = subprocess.run(["openssl", "passwd", "-apr1", "-stdin"], input=token.encode(), capture_output=True, check=True).stdout.decode().strip()
    return f"parse:{h}"


def main() -> None:
    token = os.environ["PROXY_TOKEN"]
    proxy = pwd.getpwnam("proxy")
    allow = [(resolve(h), int(p)) for h, p in (a.rsplit(":", 1) for a in os.environ.get("PARSE_PROXY_ALLOW", "").split())]
    for cmd in egress_rules.rules(proxy.pw_uid, resolvers(), allow):
        subprocess.run(cmd, check=True)
    os.makedirs("/etc/squid", exist_ok=True)
    with open("/etc/squid/passwd", "w") as f:
        f.write(passwd_line(token) + "\n")
    with open("/etc/squid/squid.conf", "w") as f:
        f.write(CONF.format(passwd="/etc/squid/passwd"))
    os.chmod("/etc/squid/passwd", 0o644)  # a SHA-1 of the token, in a container that runs nothing but squid; /var/log/squid is the package's, proxy-owned
    # NET_ADMIN is gone from here on: no capabilities at all, no new privileges
    os.execvp("setpriv", ["setpriv", "--no-new-privs", f"--reuid={proxy.pw_uid}", f"--regid={proxy.pw_gid}", "--clear-groups",
                          "--bounding-set=-all", "--inh-caps=-all", "squid", "-N", "-f", "/etc/squid/squid.conf"])


if __name__ == "__main__":
    main()
