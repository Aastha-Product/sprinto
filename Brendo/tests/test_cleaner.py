from brendo.cleaner import clean_html


def test_strips_boilerplate_and_keeps_signal():               # T-CLN-1
    html = """
    <nav>Home Product Login Sign Up</nav>
    <main>
      <h1>Acme is the B2B software platform for engineering teams building fast</h1>
      <p>We are SOC 2 certified.</p>
    </main>
    <div>We use cookies to improve your experience. Accept all</div>
    <footer>© 2026 Acme Inc. All rights reserved.</footer>
    """
    out = clean_html(html)
    assert "cookie" not in out.lower()
    assert "all rights reserved" not in out.lower()
    assert "Home Product" not in out
    assert "SOC 2" in out                       # short line kept because it carries a signal
    assert "engineering teams" in out


def test_keeps_hero_inside_header_but_drops_its_nav():         # T-CLN-2 (G4)
    html = """<header><nav>Home Pricing Login</nav>
      <h1>Acme is the compliance-ready B2B SaaS platform for engineering teams</h1></header>"""
    out = clean_html(html)
    assert "compliance-ready B2B SaaS platform" in out
    assert "Pricing" not in out


def test_caps_length():                                        # T-CLN-3
    big = "<p>" + ("word " * 5000) + "</p>"
    assert len(clean_html(big)) <= 8000


def test_dedupes_repeated_blocks():
    line = "<p>Acme helps product and engineering teams ship faster with automated workflows.</p>"
    assert clean_html(line * 3).count("ship faster") == 1
