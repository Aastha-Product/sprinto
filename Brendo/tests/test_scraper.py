from urllib import robotparser

from brendo.scraper import Scraper, _looks_blocked


def _scraper_with_robots(rules: str) -> Scraper:
    s = Scraper(None, offline=False)
    rp = robotparser.RobotFileParser()
    rp.parse(rules.splitlines())
    s._robots["acme.example"] = rp
    return s


def test_robots_disallow_is_respected(monkeypatch):           # T-SCR-1
    s = _scraper_with_robots("User-agent: *\nDisallow: /careers")
    called = []
    monkeypatch.setattr(s, "_request", lambda url: called.append(url) or "<p>x</p>")
    f = s.fetch("acme.example", "/careers")
    assert f.status == "blocked" and "robots" in f.reason
    assert called == []                                        # never requested


def test_thin_page_uses_browser_fallback(monkeypatch):        # T-SCR-2
    s = _scraper_with_robots("User-agent: *\nAllow: /")
    s.use_browser = True
    monkeypatch.setattr(s, "_request", lambda url: "<div id=root></div><script>app()</script>")
    full = "<main><p>Acme is the B2B SaaS platform for engineering teams that ship faster every week.</p></main>" * 4
    monkeypatch.setattr(s, "_browser", lambda url: full)
    f = s.fetch("acme.example", "/")
    assert f.method == "browser" and "B2B SaaS platform" in f.clean


def test_http_403_is_logged_as_blocked_not_bypassed(monkeypatch):
    s = _scraper_with_robots("User-agent: *\nAllow: /")

    class Resp:
        status_code = 403

    class HTTPErr(Exception):
        response = Resp()

    def boom(url):
        raise HTTPErr("403 Forbidden")

    monkeypatch.setattr(s, "_request", boom)
    f = s.fetch("acme.example", "/")
    assert f.status == "blocked" and f.failed
    assert _looks_blocked(HTTPErr())


def test_polite_delay_between_requests_to_same_site(monkeypatch):
    s = _scraper_with_robots("User-agent: *\nAllow: /")
    s.delay = 1.0
    slept = []
    monkeypatch.setattr("brendo.scraper.time.sleep", lambda t: slept.append(t))
    monkeypatch.setattr(s, "_request", lambda url: "<p>x</p>")
    s.fetch("acme.example", "/")
    s.fetch("acme.example", "/careers")
    assert slept and slept[0] > 0.5


def test_offline_missing_page_is_a_failure(settings):
    s = Scraper(settings.samples_dir, offline=True)
    f = s.fetch("nosuch.example", "/")
    assert f.status == "missing" and f.failed


def test_robots_403_for_python_agent_is_not_treated_as_block(monkeypatch):
    """robots.txt is fetched with Brendo's UA; 4xx = no rules (RFC 9309), 5xx = deny all."""
    import httpx
    s = Scraper(None, offline=False)

    class R:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text

    monkeypatch.setattr(httpx, "get", lambda url, **kw: R(404))
    assert s.robots_allowed("a.example", "/")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: R(503))
    assert not s.robots_allowed("b.example", "/")
    seen = {}
    monkeypatch.setattr(httpx, "get", lambda url, **kw: seen.update(kw) or R(200, "User-agent: *\nDisallow: /private"))
    assert s.robots_allowed("c.example", "/") and not s.robots_allowed("c.example", "/private/x")
    assert "BrendoResearchBot" in seen["headers"]["User-Agent"]


def test_404_is_missing_not_error(monkeypatch):
    s = _scraper_with_robots("User-agent: *\nAllow: /")

    class Resp:
        status_code = 404

    class E(Exception):
        response = Resp()

    monkeypatch.setattr(s, "_request", lambda url: (_ for _ in ()).throw(E("404")))
    assert s.fetch("acme.example", "/security").status == "missing"


def test_links_are_extracted_absolute():
    from brendo.scraper import extract_links
    links = extract_links('<a href="/trust">Trust center</a> <a href="https://x.io/soc2">SOC 2</a>',
                          "https://acme.example/")
    assert ("https://acme.example/trust", "Trust center") in links
    assert ("https://x.io/soc2", "SOC 2") in links
