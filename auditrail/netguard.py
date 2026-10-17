"""Endpoint validation for black-box Agent connections (SSRF guard).

Phase 0 rules for an Agent endpoint:

- scheme must be ``http`` or ``https``; a host is required; credentials in the
  URL (``user:pass@host``) are rejected;
- the port must be in the allowlist (default 80/443 plus deployment config);
- loopback, link-local, private, reserved, multicast and unspecified
  IPv4/IPv6 addresses are rejected unless the host is explicitly allowlisted
  or private endpoints are explicitly enabled — this covers cloud metadata
  addresses such as ``169.254.169.254``;
- a hostname is resolved via DNS and **every** resolved address is validated
  again right before connecting (DNS rebinding defence);
- redirects are re-validated through :func:`redirect_guard`, so a permitted
  endpoint cannot bounce the client to a restricted address.

Unit tests monkeypatch :func:`socket.getaddrinfo`; nothing here touches the
network by itself.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Tuple
from urllib.parse import urlparse, urlunparse

DEFAULT_ALLOWED_PORTS = frozenset({80, 443})

#: Well-known cloud metadata endpoints, rejected explicitly even if a resolver
#: or IP-version nuance would otherwise let them through.
_METADATA_IPS = frozenset({"169.254.169.254", "100.100.100.200", "fd00:ec2::254"})
_METADATA_HOSTS = frozenset({"metadata.google.internal"})


class EndpointRejected(ValueError):
    """The endpoint failed the SSRF policy; the request must not be sent."""

    def __init__(self, reason: str):
        super().__init__("endpoint rejected: %s" % reason)
        self.reason = reason


@dataclass(frozen=True)
class EndpointPolicy:
    """Deployment policy for Agent endpoints."""

    allowed_hosts: Tuple[str, ...] = ()
    allow_private: bool = False
    allowed_ports: Tuple[int, ...] = tuple(sorted(DEFAULT_ALLOWED_PORTS))
    extra_metadata_ips: Tuple[str, ...] = ()
    # Injectable for tests; production code uses socket.getaddrinfo.
    resolver: Optional[Callable[[str, int], Iterable]] = field(default=None, compare=False)

    def resolve(self, host: str, port: int) -> Iterable:
        if self.resolver is not None:
            return self.resolver(host, port)
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def normalize_endpoint(raw: object) -> str:
    """Return a canonical ``scheme://host[:port]`` endpoint or raise."""
    text = str(raw or "").strip()
    if not text:
        raise EndpointRejected("endpoint is required")
    parsed = urlparse(text if "://" in text else "http://" + text)
    if parsed.scheme not in {"http", "https"}:
        raise EndpointRejected("scheme must be http or https")
    if parsed.username or parsed.password:
        raise EndpointRejected("credentials in the endpoint URL are not allowed")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise EndpointRejected("endpoint must include a host")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise EndpointRejected("invalid port")
    path = (parsed.path or "").rstrip("/")
    if parsed.params or parsed.query or parsed.fragment:
        raise EndpointRejected("endpoint must not carry params, query or fragment")
    netloc_host = "[%s]" % host if ":" in host else host
    netloc = "%s:%s" % (netloc_host, port) if port else netloc_host
    return urlunparse((parsed.scheme, netloc, path, "", "", ""))


def validate_endpoint(raw: object, policy: Optional[EndpointPolicy] = None) -> str:
    """Validate an endpoint against the SSRF policy and return it normalized.

    DNS resolution happens here, immediately before the caller connects, and
    every resolved address is checked.
    """
    policy = policy or EndpointPolicy()
    endpoint = normalize_endpoint(raw)
    parsed = urlparse(endpoint)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    if port not in set(policy.allowed_ports):
        raise EndpointRejected("port %s is not in the allowlist" % port)

    if host in _METADATA_HOSTS:
        raise EndpointRejected("cloud metadata host is not allowed")

    explicit = host in {item.lower() for item in policy.allowed_hosts}
    literal_ip = _parse_ip(host)
    if literal_ip is not None:
        _validate_ip(literal_ip, policy, explicit)
        return endpoint

    if explicit:
        return endpoint  # explicitly allowlisted hostname; still port-checked
    if not policy.allow_private:
        # Resolve and validate every address to stop DNS rebinding to
        # loopback/private/link-local/metadata addresses.
        try:
            infos = policy.resolve(host, port)
        except (socket.gaierror, OSError) as exc:
            raise EndpointRejected("cannot resolve host %s" % host) from exc
        addresses = {_parse_ip(info[4][0]) for info in infos if info and len(info) >= 5}
        addresses.discard(None)
        if not addresses:
            raise EndpointRejected("cannot resolve host %s" % host)
        for address in addresses:
            _validate_ip(address, policy, False)
    return endpoint


def redirect_guard(policy: Optional[EndpointPolicy] = None) -> Callable[[str], None]:
    """Return a guard callable that validates a redirect target URL."""

    def guard(url: str) -> None:
        validate_endpoint(url, policy)

    return guard


def _parse_ip(host: str) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _validate_ip(address: ipaddress._BaseAddress, policy: EndpointPolicy, explicit: bool) -> None:
    text = str(address)
    if text in _METADATA_IPS or text in set(policy.extra_metadata_ips):
        raise EndpointRejected("cloud metadata address %s is not allowed" % text)
    if explicit or policy.allow_private:
        return
    if address.is_loopback:
        raise EndpointRejected("loopback address %s is not allowed" % text)
    if address.is_link_local:
        raise EndpointRejected("link-local address %s is not allowed" % text)
    if address.is_private:
        raise EndpointRejected("private address %s is not allowed" % text)
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        raise EndpointRejected("address %s is not a public unicast address" % text)
