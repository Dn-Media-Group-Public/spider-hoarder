import os

# the gateway imports its driver (the backend named by BACKEND) and reads these at import
for k, v in dict(BACKEND="browser_clear", BLOB_TOKEN_SECRET="test-secret", PUBLIC_HOST="h.example",
                 OIDC_ISSUER="https://kc.example/realms/r", REPLICA_RID="10-0-0-5").items():
    os.environ.setdefault(k, v)
