"""Per-job store credentials for components that touch the store themselves (the parser wrapper, docs/plans/parser-wrapper.md):
limited to read `projects/<proj>/data/*` and write one job's directory `projects/<proj>/data/<session>/<job>/*`.

S3 / SeaweedFS: a new role per job whose own policy is exactly that (no broad role behind it, no session policy to forget), the
gateway assumes it, and the temporary key/secret/token go to the consumer as plain fsspec options. Deleting the role revokes the credential
at once (checked on SeaweedFS 4.53), so `release` ends it with the job and `revoke_project` with the project; `sweep` deletes roles
a crashed gateway left behind. GCS: a Credential Access Boundary (downscoped token), narrow by construction; it cannot be revoked.
Every failure raises: there is no fallback to the gateway's own credential. The gateway needs the right to create roles (SeaweedFS: Admin)."""

import json
import logging
import threading
import time
from urllib.parse import urlparse

from spider_hoarder_sdk.content import storage

log = logging.getLogger("creds")
LIFETIME = 900  # seconds: the minimum for SeaweedFS and AWS STS; the 10 min job limit + 12 min poll window fit with a re-mint on retry
ROLE_PREFIX = "parse-"
_lock = threading.Lock()
active: dict[str, tuple[str, "Scoped"]] = {}  # role name -> (project, credential) minted by this replica and not yet released


def _split(blob_url: str) -> tuple[str, str]:
    u = urlparse(blob_url)
    return u.netloc, u.path.strip("/")  # bucket, key prefix inside it ("" = none)


def _data(prefix: str, proj: str) -> str:
    return "/".join(p for p in (prefix, "projects", proj, "data") if p)


def s3_policy(bucket: str, prefix: str, proj: str, session: str, job: str) -> dict:
    """The job role's policy: read the project's data side, write and delete only in the job, list only under data/. s3: actions only."""
    p = _data(prefix, proj)
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/{p}/*"},
        {"Effect": "Allow", "Action": ["s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
         "Resource": f"arn:aws:s3:::{bucket}/{p}/{session}/{job}/*"},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}",
         "Condition": {"StringLike": {"s3:prefix": f"{p}/*"}}}]}


def cab_rules(bucket: str, prefix: str, proj: str, session: str, job: str) -> list[dict]:
    """GCS Credential Access Boundary rules (at most 10 per boundary): read under data/ (and list it), write in the job."""
    res, p = f"//storage.googleapis.com/projects/_/buckets/{bucket}", _data(prefix, proj)
    obj = f"projects/_/buckets/{bucket}/objects/"
    list_attr = "api.getAttribute('storage.googleapis.com/objectListPrefix', '')"
    return [
        {"resource": res, "permissions": ["inRole:roles/storage.objectViewer"],
         "condition": f"resource.name.startsWith('{obj}{p}/') || {list_attr}.startsWith('{p}/')"},
        {"resource": res, "permissions": ["inRole:roles/storage.objectUser"],
         "condition": f"resource.name.startsWith('{obj}{p}/{session}/{job}/')"}]


class Scoped:
    """`options` go to storage.*(creds=...) / fsspec; `release()` ends the credential where the store allows it (idempotent)."""

    def __init__(self, options: dict, release=lambda: None, name: str = ""):
        self.options, self._release, self.name = options, release, name

    def release(self) -> None:
        with _lock:
            active.pop(self.name, None)
        self._release()
        self._release = lambda: None


def _aws(service: str):
    import boto3  # optional extra: gateways on an S3-compatible store need it (spider-hoarder-sdk[s3])
    o = storage.BLOB_OPTIONS
    ck = o.get("client_kwargs") or {}
    return boto3.client(service, endpoint_url=o.get("endpoint_url") or ck.get("endpoint_url"), region_name=ck.get("region_name") or "us-east-1",
                        aws_access_key_id=o.get("key"), aws_secret_access_key=o.get("secret"), aws_session_token=o.get("token"))


def _delete_role(iam, name: str) -> None:
    for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
        iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
        iam.delete_policy(PolicyArn=p["PolicyArn"])
    iam.delete_role(RoleName=name)


def mint(proj: str, session: str, job: str) -> Scoped:
    scheme = storage.BLOB_URL.split("://", 1)[0]
    bucket, prefix = _split(storage.BLOB_URL)
    if scheme in ("s3", "s3a"):
        iam, sts = _aws("iam"), _aws("sts")
        name = f"{ROLE_PREFIX}{job}"
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                 "Principal": {"AWS": sts.get_caller_identity()["Arn"]}}]}  # the gateway's own identity
        role = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        try:
            pol = iam.create_policy(PolicyName=name, PolicyDocument=json.dumps(s3_policy(bucket, prefix, proj, session, job),
                                                                                separators=(",", ":")))["Policy"]["Arn"]
            iam.attach_role_policy(RoleName=name, PolicyArn=pol)
            c = sts.assume_role(RoleArn=role, RoleSessionName=name[:64], DurationSeconds=LIFETIME)["Credentials"]
        except BaseException:
            _delete_role(iam, name)
            raise
        s = Scoped({"key": c["AccessKeyId"], "secret": c["SecretAccessKey"], "token": c["SessionToken"]}, lambda: _delete_role(iam, name), name)
        with _lock:
            active[name] = (proj, s)
        return s
    if scheme in ("gs", "gcs"):
        import google.auth
        from google.auth import downscoped
        src, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        rules = [downscoped.AccessBoundaryRule(available_resource=r["resource"], available_permissions=r["permissions"],
                                               availability_condition=downscoped.AvailabilityCondition(r["condition"]))
                 for r in cab_rules(bucket, prefix, proj, session, job)]
        # ponytail: the token lives as long as the source credential (not settable) and cannot be revoked
        return Scoped({"token": downscoped.Credentials(src, downscoped.CredentialAccessBoundary(rules))})
    raise RuntimeError(f"no scoped credentials for {storage.BLOB_URL!r}")


def revoke_project(proj: str) -> int:
    """End every credential this replica minted for the project (S3: the roles are deleted). Other replicas' expire within LIFETIME."""
    with _lock:
        mine = [s for p, s in active.values() if p == proj]
    for s in mine:
        try:
            s.release()
        except Exception:  # noqa: BLE001
            log.exception("could not revoke %s", s.name)
    return len(mine)


def sweep(max_age: float = LIFETIME + 300) -> int:
    """Delete job roles older than any credential could still live: those a crashed gateway never released. S3 only."""
    if storage.BLOB_URL.split("://", 1)[0] not in ("s3", "s3a"):
        return 0
    iam, n = _aws("iam"), 0
    for page in iam.get_paginator("list_roles").paginate():
        for r in page["Roles"]:
            if r["RoleName"].startswith(ROLE_PREFIX) and time.time() - r["CreateDate"].timestamp() > max_age:
                try:
                    _delete_role(iam, r["RoleName"])
                    with _lock:
                        active.pop(r["RoleName"], None)
                    n += 1
                except Exception:  # noqa: BLE001
                    log.exception("sweep: could not delete %s", r["RoleName"])
    return n
