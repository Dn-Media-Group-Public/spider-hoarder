"""Keycloak JWT verification for the MCP endpoint (email, optional group and scope; the audience is not checked)."""

import asyncio
import logging
import os

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

log = logging.getLogger("auth")


class KeycloakVerifier(TokenVerifier):
    def __init__(self, issuer: str, scope: str = "", group: str = "", signing_key=None, jwks_url: str = ""):
        self.issuer, self.scope, self.group = issuer.rstrip("/"), scope, group
        # signing_key: test hook, token -> key object (default: Keycloak's JWKS)
        self.signing_key = signing_key or PyJWKClient(
            jwks_url or f"{self.issuer}/protocol/openid-connect/certs", cache_keys=True
        ).get_signing_key_from_jwt

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = await asyncio.to_thread(self.signing_key, token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256", "PS256", "ES256"],
                issuer=self.issuer,
                # ponytail: aud is not validated, Claude's tokens carry arbitrary audiences
                options={"require": ["exp", "iss", "sub"], "verify_aud": False},
            )
        except Exception as e:  # noqa: BLE001
            log.info("token rejected: %s", e)
            return None
        if self.scope and self.scope not in claims.get("scope", "").split():
            log.info("token rejected: %s lacks scope %s", claims.get("email"), self.scope)
            return None
        if self.group and self.group not in claims.get("groups", []):
            log.info("token rejected: %s not in %s", claims.get("email"), self.group)
            return None
        if not claims.get("email"):
            log.info("token rejected: no email claim")
            return None
        return AccessToken(
            token=token, client_id=claims.get("azp", ""), scopes=claims.get("scope", "").split(),
            expires_at=claims.get("exp"), subject=claims["sub"], claims=claims,
        )
