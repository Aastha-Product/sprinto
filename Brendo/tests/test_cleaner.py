from brendo.cleaner import clean_html


def test_strips_boilerplate_and_keeps_signal():
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
    assert "SOC 2" in out                       # short line kept because it carries a signal
    assert "engineering teams" in out


def test_caps_length():
    big = "<p>" + ("word " * 5000) + "</p>"
    assert len(clean_html(big)) <= 8000
