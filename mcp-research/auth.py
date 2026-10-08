"""Keycloak JWT verification for the MCP endpoint (aud, email, optional group)."""

import asyncio
import logging
import os

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

log = logging.getLogger("auth")


class KeycloakVerifier(TokenVerifier):
    def __init__(self, issuer: str, audience: str, group: str = "", signing_key=None, jwks_url: str = ""):
        self.issuer, self.audience, self.group = issuer.rstrip("/"), audience, group
        # signing_key: test hook, token -> key object (default: Keycloak's JWKS)
        self.signing_key = signing_key or PyJWKClient(
            jwks_url or f"{self.issuer}/protocol/openid-connect/certs", cache_keys=True
        ).get_signing_key_from_jwt

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = await asyncio.to_thread(self.signing_key, token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256", "PS256", "ES256"],
                audience=self.audience, issuer=self.issuer,
                options={"require": ["exp", "iss", "sub", "aud"]},
            )
        except Exception as e:  # noqa: BLE001
            log.info("token rejected: %s", e)
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
