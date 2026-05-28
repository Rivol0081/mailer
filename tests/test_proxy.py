from mailer.proxy import parse, ProxyParseError
import pytest


def test_form_a_login_at_host():
    p = parse("user:pass@1.2.3.4:1080")
    assert (p.scheme, p.host, p.port, p.username, p.password) == ("http", "1.2.3.4", 1080, "user", "pass")


def test_form_b_host_at_login():
    p = parse("1.2.3.4:1080@user:pass")
    assert (p.host, p.port, p.username, p.password) == ("1.2.3.4", 1080, "user", "pass")


def test_form_c_login_pass_host_port():
    p = parse("user:pass:1.2.3.4:1080")
    assert (p.host, p.port, p.username, p.password) == ("1.2.3.4", 1080, "user", "pass")


def test_form_d_host_port_login_pass():
    p = parse("1.2.3.4:1080:user:pass")
    assert (p.host, p.port, p.username, p.password) == ("1.2.3.4", 1080, "user", "pass")


def test_no_auth():
    p = parse("proxy.example.com:8080")
    assert (p.host, p.port, p.username, p.password) == ("proxy.example.com", 8080, None, None)


def test_scheme_prefix_socks5():
    p = parse("socks5://user:pass@1.2.3.4:1080")
    assert p.scheme == "socks5"


def test_scheme_prefix_https():
    p = parse("https://1.2.3.4:443:user:pass")
    assert p.scheme == "https"


def test_unsupported_scheme():
    with pytest.raises(ProxyParseError):
        parse("ftp://1.2.3.4:21")


def test_invalid_port():
    with pytest.raises(ProxyParseError):
        parse("1.2.3.4:99999")


def test_garbage():
    with pytest.raises(ProxyParseError):
        parse("nonsense")


def test_url_property_quotes_credentials():
    p = parse("u@s:p w@1.2.3.4:1080")
    # '@' inside username should round-trip via URL-encoding
    assert "@1.2.3.4:1080" in p.url
    assert p.url.startswith("http://")


def test_short_hides_password():
    p = parse("user:secret@1.2.3.4:1080")
    assert "secret" not in p.short
    assert "user" in p.short
