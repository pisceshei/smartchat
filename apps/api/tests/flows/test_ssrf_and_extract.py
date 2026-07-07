"""external_request SSRF guard + JSONPath extraction (plan B.1)."""
from __future__ import annotations

import pytest

from apps.flow_engine import actions


# --------------------------------------------------------------------------
# blocked IP classification
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",       # loopback
        "10.0.0.5",        # private
        "192.168.1.1",     # private
        "172.16.0.1",      # private
        "169.254.169.254", # link-local (cloud metadata!)
        "::1",             # ipv6 loopback
        "fc00::1",         # ipv6 unique-local
        "fe80::1",         # ipv6 link-local
        "0.0.0.0",         # unspecified
        "224.0.0.1",       # multicast
        "not-an-ip",       # unparseable → blocked
    ],
)
def test_blocked_ips(ip):
    assert actions.is_blocked_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2001:4860:4860::8888"])
def test_public_ips_allowed(ip):
    assert not actions.is_blocked_ip(ip)


# --------------------------------------------------------------------------
# assert_public_url (resolver monkeypatched — never hits the network)
# --------------------------------------------------------------------------
def test_rejects_non_http_scheme():
    with pytest.raises(actions.SsrfBlocked):
        actions.assert_public_url("file:///etc/passwd")
    with pytest.raises(actions.SsrfBlocked):
        actions.assert_public_url("ftp://example.com/x")


def test_rejects_private_resolution(monkeypatch):
    monkeypatch.setattr(actions, "resolve_host_ips", lambda host: ["10.0.0.9"])
    with pytest.raises(actions.SsrfBlocked):
        actions.assert_public_url("https://evil.internal/api")


def test_rejects_metadata_ip(monkeypatch):
    monkeypatch.setattr(actions, "resolve_host_ips", lambda host: ["169.254.169.254"])
    with pytest.raises(actions.SsrfBlocked):
        actions.assert_public_url("http://metadata/latest")


def test_allows_public_resolution(monkeypatch):
    monkeypatch.setattr(actions, "resolve_host_ips", lambda host: ["8.8.8.8"])
    actions.assert_public_url("https://api.example.com/v1")  # no raise


def test_rejects_when_any_record_is_private(monkeypatch):
    # DNS returning a public AND a private A record → still blocked
    monkeypatch.setattr(actions, "resolve_host_ips", lambda host: ["8.8.8.8", "127.0.0.1"])
    with pytest.raises(actions.SsrfBlocked):
        actions.assert_public_url("https://mixed.example.com/x")


# --------------------------------------------------------------------------
# JSONPath extraction
# --------------------------------------------------------------------------
def test_json_extract_dotted():
    data = {"a": {"b": {"c": 42}}}
    assert actions.json_extract(data, "$.a.b.c") == 42
    assert actions.json_extract(data, "a.b.c") == 42


def test_json_extract_array_index():
    data = {"items": [{"id": 1}, {"id": 2}]}
    assert actions.json_extract(data, "$.items[1].id") == 2
    assert actions.json_extract(data, "items[0].id") == 1


def test_json_extract_missing_is_none():
    assert actions.json_extract({"a": 1}, "$.a.b.c") is None
    assert actions.json_extract({"items": []}, "$.items[3]") is None


def test_json_extract_root():
    assert actions.json_extract({"x": 1}, "$") == {"x": 1}
