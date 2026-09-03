"""The stylesheet cache-buster must match the stylesheet.

nginx serves /static/ with a 1-day cache, so a CSS change is invisible to
anyone who has already loaded the page until that expires. index.html itself
revalidates (ETag, no max-age), so versioning the <link> is what makes a change
propagate immediately.

That only works if the version is actually bumped. This test removes the
footgun: edit style.css without updating ?v= and the suite fails, rather than
the change silently not reaching browsers.
"""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _expected_version() -> str:
    return hashlib.sha256((ROOT / "static" / "style.css").read_bytes()).hexdigest()[:8]


def test_stylesheet_version_matches_stylesheet_contents():
    html = (ROOT / "static" / "index.html").read_text()
    match = re.search(r'href="/static/style\.css\?v=([0-9a-f]+)"', html)
    assert match, "index.html must load style.css with a ?v= cache-buster"
    assert match.group(1) == _expected_version(), (
        "style.css changed but its ?v= in index.html was not updated — browsers "
        "would keep the cached stylesheet for up to a day. Set it to "
        f"?v={_expected_version()}"
    )
