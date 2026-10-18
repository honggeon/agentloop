"""Phase 5 OIDC/JWT token verification.

Server-side verification only; every check is explicit:

- issuer / audience / exp / nbf validated on every token (PyJWT), with a
  configurable clock skew;
- RS256 keys come from the provider JWKS, cached for
  ``oidc_jwks_cache_seconds``; an unknown ``kid`` triggers one forced
  refresh (key rotation). When the provider is unreachable the cached JWKS
  may keep validating tokens for at most ``oidc_jwks_max_stale_seconds``
  after the last successful fetch — expired trust is never extended
  indefinitely, and new logins fail explicitly;
- HS256 (``jwt`` mode with ``AUDITRAIL_JWT_SECRET``) never touches the
  network;
- the issuer URL is operator-owned configuration; only ``https`` is
  accepted (``http`` is allowed for loopback development providers);
- tokens never enter logs, audit events, metrics or error details.

All network access goes through an injectable fetcher/poster so tests use
a mock provider — never a real OIDC service.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode, urlparse

from backend.app.config import Settings

_FETCH_TIMEOUT = 5.0


class OidcError(Exception):
    """Structured auth failure. ``status`` is 401 (token problem) or 503
    (provider unavailable for a new login / unverifiable key)."""

    def __init__(self, code: str, message: str, *, status: int = 401, retryable: bool = False) -> None:
        super().__init__(str(message)[:200])
        self.code = str(code)
        self.status = int(status)
        self.retryable = bool(retryable)


def _default_fetcher(url: str) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT) as response:  # noqa: S310 - operator-configured URL
        return json.loads(response.read().decode("utf-8"))


def _default_poster(url: str, form: Dict[str, str]) -> Dict[str, Any]:
    body = urlencode(form).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT) as response:  # noqa: S310 - operator-configured URL
        return json.loads(response.read().decode("utf-8"))


def validate_issuer_url(url: str) -> str:
    """Normalize and policy-check the configured issuer URL."""
    text = str(url or "").strip().rstrip("/")
    if not text:
        raise OidcError("auth_misconfigured", "OIDC issuer URL is required", status=500)
    parsed = urlparse(text)
    if parsed.scheme == "https":
        return text
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return text  # loopback development provider only
    raise OidcError("auth_misconfigured", "OIDC issuer URL must be https (http is loopback-only)", status=500)


class OidcProvider:
    """Discovery + JWKS + token verification for one configured issuer."""

    def __init__(
        self,
        settings: Settings,
        *,
        fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
        poster: Optional[Callable[[str, Dict[str, str]], Dict[str, Any]]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._fetcher = fetcher or _default_fetcher
        self._poster = poster or _default_poster
        self._clock = clock
        self._discovery: Optional[Dict[str, Any]] = None
        self._jwks: Optional[Dict[str, Any]] = None
        self._jwks_fetched_at: Optional[float] = None
        self._jwks_last_success_at: Optional[float] = None

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------

    @property
    def issuer(self) -> str:
        return validate_issuer_url(self._settings.oidc_issuer_url)

    @property
    def audience(self) -> Optional[str]:
        return (
            str(self._settings.oidc_audience or "").strip()
            or str(self._settings.oidc_client_id or "").strip()
            or None
        )

    # ------------------------------------------------------------------
    # discovery / JWKS
    # ------------------------------------------------------------------

    def discovery(self) -> Dict[str, Any]:
        if self._discovery is not None:
            return self._discovery
        url = "%s/.well-known/openid-configuration" % self.issuer
        try:
            document = self._fetcher(url)
        except Exception as exc:
            raise OidcError(
                "provider_unavailable", "OIDC discovery failed", status=503, retryable=True
            ) from exc
        if not isinstance(document, dict):
            raise OidcError("provider_unavailable", "OIDC discovery returned an invalid document", status=503, retryable=True)
        self._discovery = document
        return document

    def _jwks_url(self) -> str:
        document = self.discovery()
        url = str(document.get("jwks_uri") or "").strip()
        if not url:
            raise OidcError("provider_unavailable", "OIDC discovery has no jwks_uri", status=503, retryable=True)
        parsed = urlparse(url)
        if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise OidcError("auth_misconfigured", "jwks_uri must be https", status=500)
        return url

    def jwks(self, *, force_refresh: bool = False) -> Dict[str, Any]:
        now = self._clock()
        cache_seconds = int(self._settings.oidc_jwks_cache_seconds)
        max_stale = int(self._settings.oidc_jwks_max_stale_seconds)
        if (
            not force_refresh
            and self._jwks is not None
            and self._jwks_fetched_at is not None
            and now - self._jwks_fetched_at < cache_seconds
        ):
            return self._jwks
        try:
            document = self._fetcher(self._jwks_url())
            if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
                raise ValueError("invalid JWKS document")
        except Exception as exc:
            # Provider unreachable: cached keys may keep validating tokens
            # for at most max_stale after the last *successful* fetch.
            if (
                self._jwks is not None
                and self._jwks_last_success_at is not None
                and now - self._jwks_last_success_at < max_stale
            ):
                self._jwks_fetched_at = now  # do not hammer the provider
                return self._jwks
            raise OidcError(
                "provider_unavailable",
                "JWKS unavailable and no usable cached keys",
                status=503,
                retryable=True,
            ) from exc
        self._jwks = document
        self._jwks_fetched_at = now
        self._jwks_last_success_at = now
        return document

    def _key_for(self, kid: Optional[str], alg: str) -> Any:
        try:
            from jwt.algorithms import RSAAlgorithm
        except ImportError as exc:  # pragma: no cover - guarded by extra deps
            raise OidcError("auth_misconfigured", "PyJWT is required for OIDC", status=500) from exc

        def find(document: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            keys = document.get("keys") or []
            candidates = [k for k in keys if isinstance(k, dict)]
            if kid:
                candidates = [k for k in candidates if k.get("kid") == kid]
            use_ok = [k for k in candidates if k.get("use") in (None, "sig")]
            return (use_ok or candidates or [None])[0]

        jwk = find(self.jwks())
        if jwk is None and kid:
            # Unknown kid: key rotation — refresh once, then fail closed.
            jwk = find(self.jwks(force_refresh=True))
        if jwk is None:
            raise OidcError("token_invalid", "no matching signing key", status=401)
        try:
            return RSAAlgorithm.from_jwk(json.dumps(jwk))
        except Exception as exc:
            raise OidcError("token_invalid", "unsupported signing key", status=401) from exc

    # ------------------------------------------------------------------
    # verification
    # ------------------------------------------------------------------

    def verify_token(self, token: str) -> Dict[str, Any]:
        """Verify a Bearer token; returns the validated claims."""
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - guarded by extra deps
            raise OidcError("auth_misconfigured", "PyJWT is required for OIDC/JWT auth", status=500) from exc

        text = str(token or "").strip()
        if not text:
            raise OidcError("token_missing", "missing bearer token", status=401)
        try:
            header = jwt.get_unverified_header(text)
        except Exception as exc:
            raise OidcError("token_invalid", "malformed token", status=401) from exc
        alg = str(header.get("alg") or "")
        allowed = tuple(self._settings.jwt_algorithms or ("RS256", "HS256"))
        if alg not in allowed:
            raise OidcError("token_invalid", "disallowed token algorithm", status=401)

        if alg.startswith("HS"):
            secret = str(self._settings.jwt_secret or "")
            if not secret:
                raise OidcError("auth_misconfigured", "HS256 requires AUDITRAIL_JWT_SECRET", status=500)
            key: Any = secret
        else:
            key = self._key_for(header.get("kid"), alg)

        issuer = str(self._settings.oidc_issuer_url or "").strip()
        kwargs: Dict[str, Any] = {
            "algorithms": [alg],
            "leeway": int(self._settings.oidc_clock_skew_seconds),
        }
        audience = self.audience
        if audience:
            kwargs["audience"] = audience
        else:
            kwargs["options"] = {"verify_aud": False}
        if issuer:
            kwargs["issuer"] = validate_issuer_url(issuer)
        try:
            claims = jwt.decode(text, key, **kwargs)
        except jwt.ExpiredSignatureError as exc:
            raise OidcError("token_expired", "token expired", status=401) from exc
        except jwt.ImmatureSignatureError as exc:
            raise OidcError("token_not_yet_valid", "token not yet valid (nbf)", status=401) from exc
        except jwt.InvalidIssuerError as exc:
            raise OidcError("issuer_mismatch", "token issuer mismatch", status=401) from exc
        except jwt.InvalidAudienceError as exc:
            raise OidcError("audience_mismatch", "token audience mismatch", status=401) from exc
        except OidcError:
            raise
        except Exception as exc:
            raise OidcError("token_invalid", "token verification failed", status=401) from exc
        if not isinstance(claims, dict) or not claims.get("sub"):
            raise OidcError("token_invalid", "token has no subject", status=401)
        return claims

    # ------------------------------------------------------------------
    # roles / identity mapping
    # ------------------------------------------------------------------

    def extract_roles(self, claims: Dict[str, Any]) -> List[str]:
        """Recognized roles from the configured claim (then ``groups``).

        Unknown values are ignored — an unrecognized role can never grant
        anything. The result may be empty; callers map that to the least
        privileged role.
        """
        from backend.app.auth import ROLES

        raw = claims.get(self._settings.oidc_roles_claim)
        if raw is None:
            raw = claims.get("groups")
        values: List[str] = []
        if isinstance(raw, str):
            values = [part for part in raw.replace(",", " ").split() if part]
        elif isinstance(raw, list):
            values = [str(item) for item in raw]
        recognized = []
        for value in values:
            role = value.strip().lower()
            if role in ROLES and role not in recognized:
                recognized.append(role)
        return recognized

    # ------------------------------------------------------------------
    # browser login flow (authorization code)
    # ------------------------------------------------------------------

    def build_login_url(self, *, state: str, nonce: str, redirect_uri: str) -> str:
        endpoint = str(self.discovery().get("authorization_endpoint") or "").strip()
        if not endpoint:
            raise OidcError("provider_unavailable", "OIDC discovery has no authorization_endpoint", status=503, retryable=True)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": str(self._settings.oidc_client_id or ""),
                "redirect_uri": redirect_uri,
                "scope": str(self._settings.oidc_scopes or "openid").replace(",", " "),
                "state": state,
                "nonce": nonce,
            }
        )
        return "%s?%s" % (endpoint, query)

    def exchange_code(self, *, code: str, redirect_uri: str) -> Dict[str, Any]:
        endpoint = str(self.discovery().get("token_endpoint") or "").strip()
        if not endpoint:
            raise OidcError("provider_unavailable", "OIDC discovery has no token_endpoint", status=503, retryable=True)
        form = {
            "grant_type": "authorization_code",
            "code": str(code),
            "redirect_uri": redirect_uri,
            "client_id": str(self._settings.oidc_client_id or ""),
        }
        if self._settings.oidc_client_secret:
            form["client_secret"] = str(self._settings.oidc_client_secret)
        try:
            result = self._poster(endpoint, form)
        except Exception as exc:
            raise OidcError("login_failed", "authorization code exchange failed", status=401) from exc
        if not isinstance(result, dict) or not result.get("id_token"):
            raise OidcError("login_failed", "token endpoint returned no id_token", status=401)
        return result


_PROVIDERS: Dict[int, OidcProvider] = {}


def provider_for(settings: Settings) -> OidcProvider:
    """Process-wide provider per settings object (keeps the JWKS cache)."""
    key = id(settings)
    provider = _PROVIDERS.get(key)
    if provider is None:
        provider = OidcProvider(settings)
        _PROVIDERS[key] = provider
    return provider


def reset_providers() -> None:
    _PROVIDERS.clear()
