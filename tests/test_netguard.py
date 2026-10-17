"""SSRF endpoint policy: scheme, credentials, ports, restricted IPs, DNS rebinding, redirects."""

import pytest

from auditrail.netguard import (
    EndpointPolicy,
    EndpointRejected,
    normalize_endpoint,
    redirect_guard,
    validate_endpoint,
)


def _public_resolver(host, port):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def _rebinding_resolver(host, port):
    # DNS answers a loopback address — the classic rebinding attack.
    return [(2, 1, 6, "", ("127.0.0.1", port))]


def _policy(**kwargs):
    kwargs.setdefault("resolver", _public_resolver)
    kwargs.setdefault("allowed_ports", (80, 443, 8788))
    return EndpointPolicy(**kwargs)


def test_accepts_plain_public_http_endpoint():
    assert validate_endpoint("http://agent.example:8788", _policy()) == "http://agent.example:8788"


def test_rejects_non_http_scheme():
    with pytest.raises(EndpointRejected):
        validate_endpoint("ftp://agent.example", _policy())
    with pytest.raises(EndpointRejected):
        validate_endpoint("file:///etc/passwd", _policy())


def test_rejects_missing_host_and_credentials():
    with pytest.raises(EndpointRejected):
        validate_endpoint("http://", _policy())
    with pytest.raises(EndpointRejected):
        validate_endpoint("http://user:pass@agent.example", _policy())


def test_rejects_non_allowlisted_port():
    with pytest.raises(EndpointRejected):
        validate_endpoint("http://agent.example:22", _policy())


def test_rejects_loopback_private_linklocal_and_metadata_literals():
    for endpoint in (
        "http://127.0.0.1:8788",
        "http://[::1]:8788",
        "http://10.0.0.5",
        "http://192.168.1.238:8788",
        "http://172.16.0.1",
        "http://169.254.1.1",
        "http://169.254.169.254",  # cloud metadata
        "http://100.100.100.200",  # aliyun metadata
        "http://0.0.0.0",
    ):
        with pytest.raises(EndpointRejected):
            validate_endpoint(endpoint, _policy())


def test_allowlisted_host_bypasses_ip_denial_but_not_port_check():
    policy = _policy(allowed_hosts=("192.168.1.238",))
    assert validate_endpoint("http://192.168.1.238:8788", policy) == "http://192.168.1.238:8788"
    with pytest.raises(EndpointRejected):
        validate_endpoint("http://192.168.1.238:22", policy)


def test_allow_private_flag_permits_internal_endpoints():
    policy = _policy(allow_private=True)
    assert validate_endpoint("http://10.0.0.5", policy) == "http://10.0.0.5:80"


def test_dns_rebinding_to_loopback_is_rejected():
    policy = _policy(resolver=_rebinding_resolver)
    with pytest.raises(EndpointRejected):
        validate_endpoint("http://evil.example:8788", policy)


def test_dns_resolution_failure_is_rejected():
    def failing_resolver(host, port):
        raise OSError("name resolution failed")

    with pytest.raises(EndpointRejected):
        validate_endpoint("http://gone.example", _policy(resolver=failing_resolver))


def test_metadata_hostname_is_rejected():
    with pytest.raises(EndpointRejected):
        validate_endpoint("http://metadata.google.internal", _policy())


def test_redirect_guard_revalidates_targets():
    guard = redirect_guard(_policy())
    guard("http://agent.example:8788/ok")  # no raise
    with pytest.raises(EndpointRejected):
        guard("http://127.0.0.1:8788/internal")
    with pytest.raises(EndpointRejected):
        guard("http://169.254.169.254/latest/meta-data")


def test_normalize_endpoint_strips_trailing_slash_and_defaults_port():
    assert normalize_endpoint("http://agent.example/") == "http://agent.example:80"
    assert normalize_endpoint("https://agent.example") == "https://agent.example:443"


def test_normalize_endpoint_rejects_query_and_fragment():
    with pytest.raises(EndpointRejected):
        normalize_endpoint("http://agent.example/?x=1")
