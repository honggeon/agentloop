"""Phase 5 OIDC/JWT authentication: issuer/audience/expiry/nbf validation,
JWKS caching and key rotation, 401 vs 403, role mapping, and the
production-startup guard. All provider access goes through an injected
fetcher — no real OIDC provider, no network.
"""

import json
import time

import pytest

jwt = pytest.importorskip("jwt", reason="PyJWT optional dependency")
cryptography = pytest.importorskip("cryptography", reason="cryptography optional dependency")

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from backend.app import oidc
from backend.app.config import Settings, validate_startup_settings
from backend.app.main import app
from backend.app.oidc import OidcError, OidcProvider

ISSUER = "https://issuer.example"
AUDIENCE = "auditrail"


def _rsa_pair(kid):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return key, jwk


class FakeProvider:
    """Scriptable OIDC provider: discovery + JWKS via fetcher calls."""

    def __init__(self):
        self.keys = {}
        self.fetch_calls = []
        self.down = False

    def add_key(self, kid):
        key, jwk = _rsa_pair(kid)
        self.keys[kid] = key
        return key

    def fetcher(self, url):
        self.fetch_calls.append(url)
        if self.down:
            raise ConnectionError("provider down")
        if url.endswith("/.well-known/openid-configuration"):
            return {
                "issuer": ISSUER,
                "jwks_uri": ISSUER + "/jwks",
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
            }
        if url.endswith("/jwks"):
            kids = []
            for kid, key in self.keys.items():
                jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
                jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
                kids.append(jwk)
            return {"keys": kids}
        raise AssertionError("unexpected fetch %s" % url)


def _settings(tmp_path, **overrides):
    base = dict(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        auth_mode="oidc",
        oidc_issuer_url=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_cache_seconds=3600,
        oidc_jwks_max_stale_seconds=120,
    )
    base.update(overrides)
    settings = Settings(**base)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    settings.platform_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _token(key, kid, *, sub="user-1", roles=("viewer",), iss=ISSUER, aud=AUDIENCE, exp_in=3600, nbf=None, **extra):
    now = int(time.time())
    claims = {"sub": sub, "iss": iss, "aud": aud, "iat": now, "exp": now + exp_in, "roles": list(roles)}
    if nbf is not None:
        claims["nbf"] = nbf
    claims.update(extra)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def _client(monkeypatch, settings, provider):
    for module in (
        "backend.app.routes.integrations",
        "backend.app.routes.admin",
        "backend.app.routes.platform",
        "backend.app.routes.auth_admin",
        "backend.app.auth_middleware",
    ):
        monkeypatch.setattr("%s.get_settings" % module, lambda: settings)
    oidc_provider = OidcProvider(settings, fetcher=provider.fetcher)
    monkeypatch.setattr("backend.app.oidc.provider_for", lambda settings: oidc_provider)
    return TestClient(app), oidc_provider


# ---------------------------------------------------------------------------
# startup guard
# ---------------------------------------------------------------------------


def test_production_with_disabled_auth_fails_startup(tmp_path):
    settings = _settings(
        tmp_path, auth_mode="disabled", deployment_mode="production", storage_mode="database",
        database_url="postgresql://localhost/db",
    )
    with pytest.raises(ValueError) as excinfo:
        validate_startup_settings(settings)
    assert "auth" in str(excinfo.value)


def test_production_requires_database_storage(tmp_path):
    settings = _settings(tmp_path, deployment_mode="production", storage_mode="file")
    with pytest.raises(ValueError):
        validate_startup_settings(settings)


def test_oidc_without_issuer_fails_validation(tmp_path):
    settings = _settings(tmp_path, oidc_issuer_url="", jwt_secret="")
    with pytest.raises(ValueError):
        validate_startup_settings(settings)


def test_https_only_issuer(tmp_path):
    provider = OidcProvider(_settings(tmp_path, oidc_issuer_url="http://evil.example"))
    with pytest.raises(OidcError):
        _ = provider.issuer
    loopback = OidcProvider(_settings(tmp_path, oidc_issuer_url="http://localhost:8080"))
    assert loopback.issuer == "http://localhost:8080"


# ---------------------------------------------------------------------------
# token verification
# ---------------------------------------------------------------------------


def test_valid_token_verifies(tmp_path):
    fake = FakeProvider()
    key = fake.add_key("k1")
    provider = OidcProvider(_settings(tmp_path), fetcher=fake.fetcher)
    claims = provider.verify_token(_token(key, "k1"))
    assert claims["sub"] == "user-1"


def test_wrong_issuer_rejected(tmp_path):
    fake = FakeProvider()
    key = fake.add_key("k1")
    provider = OidcProvider(_settings(tmp_path), fetcher=fake.fetcher)
    with pytest.raises(OidcError) as excinfo:
        provider.verify_token(_token(key, "k1", iss="https://evil.example"))
    assert excinfo.value.code == "issuer_mismatch"
    assert excinfo.value.status == 401


def test_wrong_audience_rejected(tmp_path):
    fake = FakeProvider()
    key = fake.add_key("k1")
    provider = OidcProvider(_settings(tmp_path), fetcher=fake.fetcher)
    with pytest.raises(OidcError) as excinfo:
        provider.verify_token(_token(key, "k1", aud="other-service"))
    assert excinfo.value.code == "audience_mismatch"


def test_expired_token_rejected(tmp_path):
    fake = FakeProvider()
    key = fake.add_key("k1")
    provider = OidcProvider(_settings(tmp_path), fetcher=fake.fetcher)
    with pytest.raises(OidcError) as excinfo:
        provider.verify_token(_token(key, "k1", exp_in=-3600))
    assert excinfo.value.code == "token_expired"
    assert excinfo.value.status == 401


def test_nbf_and_clock_skew(tmp_path):
    fake = FakeProvider()
    key = fake.add_key("k1")
    now = int(time.time())
    # future nbf beyond skew: rejected
    strict = OidcProvider(_settings(tmp_path, oidc_clock_skew_seconds=30), fetcher=fake.fetcher)
    with pytest.raises(OidcError) as excinfo:
        strict.verify_token(_token(key, "k1", nbf=now + 3600))
    assert excinfo.value.code == "token_not_yet_valid"
    # same token accepted with a wide configured skew
    lenient = OidcProvider(_settings(tmp_path, oidc_clock_skew_seconds=7200), fetcher=fake.fetcher)
    assert lenient.verify_token(_token(key, "k1", nbf=now + 3600))["sub"] == "user-1"


def test_jwks_cached_and_rotation_refreshes(tmp_path):
    fake = FakeProvider()
    key1 = fake.add_key("k1")
    provider = OidcProvider(_settings(tmp_path), fetcher=fake.fetcher)
    provider.verify_token(_token(key1, "k1"))
    jwks_calls = [url for url in fake.fetch_calls if url.endswith("/jwks")]
    assert len(jwks_calls) == 1
    # second token with same kid: cache hit, no refetch
    provider.verify_token(_token(key1, "k1"))
    assert len([url for url in fake.fetch_calls if url.endswith("/jwks")]) == 1
    # rotation: new kid forces exactly one refresh
    key2 = fake.add_key("k2")
    claims = provider.verify_token(_token(key2, "k2"))
    assert claims["sub"] == "user-1"
    assert len([url for url in fake.fetch_calls if url.endswith("/jwks")]) == 2
    # retired key no longer verifies
    del fake.keys["k1"]
    provider.jwks(force_refresh=True)
    with pytest.raises(OidcError):
        provider.verify_token(_token(key1, "k1"))


def test_provider_outage_uses_bounded_stale_cache(tmp_path):
    fake = FakeProvider()
    key = fake.add_key("k1")
    clock = [1000.0]
    provider = OidcProvider(_settings(tmp_path, oidc_jwks_cache_seconds=10, oidc_jwks_max_stale_seconds=120), fetcher=fake.fetcher, clock=lambda: clock[0])
    provider.verify_token(_token(key, "k1"))  # primes the cache
    fake.down = True
    clock[0] += 60  # cache expired but within max stale window
    assert provider.verify_token(_token(key, "k1"))["sub"] == "user-1"
    clock[0] += 600  # beyond max stale: fail closed, never trust expired keys
    with pytest.raises(OidcError) as excinfo:
        provider.verify_token(_token(key, "k1"))
    assert excinfo.value.code == "provider_unavailable"
    assert excinfo.value.status == 503


def test_hs256_jwt_mode(tmp_path):
    fake = FakeProvider()
    settings = _settings(tmp_path, auth_mode="jwt", jwt_secret="test-secret", oidc_issuer_url="")
    provider = OidcProvider(settings, fetcher=fake.fetcher)
    now = int(time.time())
    token = jwt.encode({"sub": "svc-1", "aud": AUDIENCE, "iat": now, "exp": now + 60, "roles": ["operator"]}, "test-secret", algorithm="HS256")
    claims = provider.verify_token(token)
    assert claims["sub"] == "svc-1"
    # wrong secret never verifies
    bad = jwt.encode({"sub": "svc-1", "aud": AUDIENCE, "iat": now, "exp": now + 60}, "wrong", algorithm="HS256")
    with pytest.raises(OidcError):
        provider.verify_token(bad)


def test_disallowed_algorithm_rejected(tmp_path):
    fake = FakeProvider()
    fake.add_key("k1")
    settings = _settings(tmp_path, jwt_algorithms=("RS256",))  # HS256 not allowed
    provider = OidcProvider(settings, fetcher=fake.fetcher)
    now = int(time.time())
    token = jwt.encode({"sub": "x", "aud": AUDIENCE, "iat": now, "exp": now + 60}, "irrelevant", algorithm="HS256")
    with pytest.raises(OidcError) as excinfo:
        provider.verify_token(token)
    assert excinfo.value.code == "token_invalid"


# ---------------------------------------------------------------------------
# API level: 401 vs 403, role mapping, context safety
# ---------------------------------------------------------------------------


def test_api_missing_token_401_invalid_role_never_elevates(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    fake = FakeProvider()
    key = fake.add_key("k1")
    client, _provider = _client(monkeypatch, settings, fake)

    # no token on a protected route -> 401
    response = client.get("/api/agents")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "token_missing"

    # unrecognized roles cannot elevate: viewer-only
    token = _token(key, "k1", roles=("superuser", "root"))
    denied = client.post(
        "/api/agents",
        json={"agent_id": "a1", "endpoint": "http://agent.internal"},
        headers={"Authorization": "Bearer %s" % token},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "permission_denied"

    # role headers are never trusted
    spoofed = client.get(
        "/api/admin/users",
        headers={"Authorization": "Bearer %s" % token, "X-Role": "owner", "X-Tenant-Id": "other"},
    )
    assert spoofed.status_code == 403


def test_api_expired_token_401_and_valid_token_200(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    fake = FakeProvider()
    key = fake.add_key("k1")
    client, _provider = _client(monkeypatch, settings, fake)
    expired = client.get("/api/agents", headers={"Authorization": "Bearer %s" % _token(key, "k1", exp_in=-3600)})
    assert expired.status_code == 401
    assert expired.json()["detail"]["code"] == "token_expired"
    ok = client.get("/api/agents", headers={"Authorization": "Bearer %s" % _token(key, "k1")})
    assert ok.status_code == 200


def test_auth_context_never_exposes_token(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    fake = FakeProvider()
    key = fake.add_key("k1")
    client, _provider = _client(monkeypatch, settings, fake)
    token = _token(key, "k1", roles=("admin",), email="a@example.com", name="Alice")
    response = client.get("/api/auth/context", headers={"Authorization": "Bearer %s" % token})
    assert response.status_code == 200
    body = response.json()
    assert body["auth_mode"] == "oidc"
    assert body["role"] == "admin"
    assert body["authenticated"] is True
    assert body["production_ready"] is True
    assert body["user"]["email"] == "a@example.com"
    assert body["tenant"]["single_tenant"] is True
    assert token not in json.dumps(body)


def test_local_and_disabled_modes_still_work_without_token(monkeypatch, tmp_path):
    settings = _settings(tmp_path, auth_mode="local", auth_role="viewer", oidc_issuer_url="")
    fake = FakeProvider()
    client, _provider = _client(monkeypatch, settings, fake)
    assert client.get("/api/agents").status_code == 200
    denied = client.post("/api/agents", json={"agent_id": "a1", "endpoint": "http://agent.internal"})
    assert denied.status_code == 403
