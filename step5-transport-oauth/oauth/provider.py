"""MCP-level OAuth 2.1 authorization server for the Step 5 Drive server.

Implements `mcp.server.auth.provider.OAuthAuthorizationServerProvider` --
the protocol the MCP Python SDK's own `create_auth_routes` /
`RequireAuthMiddleware` wiring expects an implementation of. Wiring those
routes and middleware into a Starlette app (see ../server.py) is what
teaches the MCP-specific plumbing this step is actually about: which
endpoints exist, which headers carry what, which discovery document
points where. This class is the part the SDK deliberately leaves to the
implementor -- who the registered clients are, how consent is granted,
and how bearer tokens get minted and verified.

PKCE verification itself (matching the token request's `code_verifier`
against the `code_challenge` captured at /authorize) is NOT reimplemented
here -- `mcp.server.auth.handlers.token.TokenHandler` already does that
(SHA256 + base64url compare against RFC 7636) before
`exchange_authorization_code` below is ever called. That's the SDK doing
its job; this class starts from an already-PKCE-verified authorization
code.

Token issuance/verification uses `joserfc` -- the JOSE implementation
`authlib` itself now ships and recommends over its own older, deprecated
`authlib.jose` module -- for HS256 JWT signing and verification, so no
token cryptography is hand-rolled here. Access tokens are short-lived
signed JWTs carrying `aud` (the resource server URL), `scope`, `sub`,
and `jti` claims; `load_access_token` below checks the signature AND the
audience AND the revocation set -- a syntactically valid, correctly
signed token minted for a *different* resource is still rejected. That
distinction (a token exists vs. a token is valid for *this* resource) is
one of this step's named pitfalls, not an incidental detail.

This server acts as both the authorization server and the resource
owner's consent screen -- there is no third-party IdP to federate to
for a single-user portfolio project. `authorize()` doesn't decide
approval itself; it stashes the pending request and returns a URL to
this server's own interactive consent page (see consent.py), which
calls `complete_authorization()` only after a human actually clicks
Approve.
"""

import secrets
import time
from dataclasses import dataclass

from joserfc import jwt
from joserfc.jwk import OctKey
from joserfc.jwt import JWTClaimsRegistry
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30
AUTH_CODE_TTL_SECONDS = 300

_JWT_HEADER = {"alg": "HS256"}
_CLAIMS_REGISTRY = JWTClaimsRegistry()


@dataclass
class PendingAuthorization:
    """One not-yet-approved /authorize request, waiting on the consent screen."""

    client: OAuthClientInformationFull
    params: AuthorizationParams


class DriveOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, *, issuer_url: str, resource_url: str, signing_secret: str, consent_url: str) -> None:
        self._issuer_url = issuer_url
        self._resource_url = resource_url
        self._signing_key = OctKey.import_key(signing_secret)
        self._consent_url = consent_url

        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._pending: dict[str, PendingAuthorization] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        self._revoked_jti: set[str] = set()

    # --- dynamic client registration (RFC 7591) ----------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    # --- authorize: hand off to the interactive consent screen -------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request_id = secrets.token_urlsafe(24)
        self._pending[request_id] = PendingAuthorization(client=client, params=params)
        return f"{self._consent_url}?request_id={request_id}"

    def pop_pending(self, request_id: str) -> PendingAuthorization | None:
        """Called by the consent route (GET) to render the approve/deny form."""
        return self._pending.get(request_id)

    def complete_authorization(self, request_id: str, *, approved: bool, subject: str) -> str | None:
        """Called by the consent route (POST) once the resource owner decides.

        Returns the redirect_uri (with `code` and `state` attached on
        approval, or an error on denial) the browser should be sent to
        next, or None if request_id doesn't correspond to a pending
        request (expired/already used/forged).
        """
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return None

        if not approved:
            return construct_redirect_uri(
                str(pending.params.redirect_uri),
                error="access_denied",
                error_description="resource owner denied the request",
                state=pending.params.state,
            )

        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=pending.params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
            client_id=pending.client.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            subject=subject,
        )
        return construct_redirect_uri(str(pending.params.redirect_uri), code=code, state=pending.params.state)

    # --- authorization code -> tokens --------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._auth_codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # One-time use: gone the instant it's exchanged, whether or not
        # exchange succeeds past this point.
        self._auth_codes.pop(authorization_code.code, None)
        return self._issue_token(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            subject=authorization_code.subject,
        )

    # --- refresh tokens ------------------------------------------------------

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        token = self._refresh_tokens.get(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the old refresh token is consumed, a new one is issued
        # alongside the new access token.
        self._refresh_tokens.pop(refresh_token.token, None)
        return self._issue_token(
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            subject=refresh_token.subject,
        )

    def _issue_token(self, *, client_id: str, scopes: list[str], subject: str | None) -> OAuthToken:
        now = int(time.time())
        jti = secrets.token_urlsafe(16)
        claims = {
            "iss": self._issuer_url,
            "aud": self._resource_url,
            "sub": subject,
            "client_id": client_id,
            "scope": " ".join(scopes),
            "jti": jti,
            "iat": now,
            "exp": now + ACCESS_TOKEN_TTL_SECONDS,
        }
        access_token = jwt.encode(_JWT_HEADER, claims, self._signing_key)

        refresh_token_str = secrets.token_urlsafe(32)
        self._refresh_tokens[refresh_token_str] = RefreshToken(
            token=refresh_token_str,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + REFRESH_TOKEN_TTL_SECONDS,
            subject=subject,
        )

        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_token_str,
            scope=" ".join(scopes),
        )

    # --- verifying incoming bearer tokens (the resource-server half) -------

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            decoded = jwt.decode(token, self._signing_key)
            _CLAIMS_REGISTRY.validate(decoded.claims)
        except Exception:
            # Any signature failure, malformed token, or expiry -- all the
            # same outcome: reject. Never partially trust a token that
            # failed verification.
            return None

        claims = decoded.claims

        jti = claims.get("jti")
        if jti is not None and jti in self._revoked_jti:
            return None

        # Audience check: a syntactically valid, correctly-signed token
        # minted for a *different* resource server must not be accepted
        # here just because it verifies -- "has a bearer token" is not
        # "the token is valid for this resource". See module docstring.
        if claims.get("aud") != self._resource_url:
            return None

        scope_claim = claims.get("scope") or ""
        return AccessToken(
            token=token,
            client_id=claims.get("client_id", ""),
            scopes=scope_claim.split() if scope_claim else [],
            expires_at=claims.get("exp"),
            resource=self._resource_url,
            subject=claims.get("sub"),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            self._refresh_tokens.pop(token.token, None)
            return

        try:
            decoded = jwt.decode(token.token, self._signing_key)
        except Exception:
            return
        jti = decoded.claims.get("jti")
        if jti is not None:
            self._revoked_jti.add(jti)
