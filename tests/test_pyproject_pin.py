"""Tests for the akash-lease-core pin declared in pyproject.toml.

C5 structural review item 2 (issue #178): the akash-lease-core wheel pin must
be a well-formed release URL with a matching SHA-256 digest. A silent bump to
a non-uniformity-audited release would lock us into a core whose
AuctionPolicy contract is not verified against all three downstream
consumers — so the pin must be guarded against malformed edits.

The CONTENT of the pin (which version is selected) is governed by
docs/C5-PIN-PLAN.md and is intentionally NOT asserted here: that decision is
gated on Digital-Frontier-LDA/akash-lease-core#13 sub-item 1 (uniformity
audit). These tests assert only the shape.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Format produced by GitHub release downloads, e.g.
#   akash-lease-core @ https://github.com/.../releases/download/v0.7.0/akash_lease_core-0.7.0-py3-none-any.whl#sha256=65318a87...
_PIN_PATTERN = re.compile(
    r"""
    ^\s*                      # leading whitespace
    "akash-lease-core\s*@\s*  # package name with PEP 508 direct reference
    (?P<url>https://github\.com/      # only github.com URLs are trusted
        Digital-Frontier-LDA/
        akash-lease-core/
        releases/download/
        v(?P<version_path>\d+\.\d+\.\d+)/  # semver in URL path (with v-prefix)
        akash_lease_core-
        (?P<version_wheel>\d+\.\d+\.\d+)-  # semver in wheel filename (no v-prefix)
        py3-none-any\.whl)
    \#sha256=
    (?P<sha>[0-9a-f]{64})"   # SHA-256 hex digest
    ,?\s*$                    # optional trailing comma
    """,
    re.VERBOSE | re.MULTILINE,
)


@pytest.fixture(scope="module")
def pin_line() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    # Find the akash-lease-core pin in the dependencies block. The line is
    # quoted (PEP 508 list item), so it starts with a double-quote after the
    # leading indent — not the bare package name.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"akash-lease-core') and "akash_lease_core-" in stripped:
            return line
    pytest.fail(f"akash-lease-core pin not found in {PYPROJECT}")


def test_pin_is_a_quoted_string(pin_line: str) -> None:
    """The pin must be wrapped in double quotes so hatchling accepts the URL."""
    assert pin_line.lstrip().startswith('"'), f"pin must be quoted, got: {pin_line!r}"
    assert pin_line.rstrip().endswith(","), (
        f"pin must end with a comma (PEP 508 list item), got: {pin_line!r}"
    )


def test_pin_points_to_digital_frontier_lda(pin_line: str) -> None:
    """Only github.com/Digital-Frontier-LDA/akash-lease-core releases are trusted.

    A future contributor reaching for a fork or an artifact-registry URL is
    almost always wrong: the project policy is to pin the wheel this org
    publishes. The regex captures the full URL so any deviation fails.
    """
    match = _PIN_PATTERN.match(pin_line)
    assert match is not None, (
        f"akash-lease-core pin does not match the expected release-wheel "
        f"shape:\n  got: {pin_line!r}\n"
        f"  expected: "
        f'"akash-lease-core @ https://github.com/Digital-Frontier-LDA/'
        f"akash-lease-core/releases/download/vX.Y.Z/"
        f'akash_lease_core-X.Y.Z-py3-none-any.whl#sha256=<64-hex>"'
    )
    url = match.group("url")
    # ⚠ Compare the HOST, not a substring. `"github.com" in url` is satisfied by
    # `https://evil.example/?r=github.com` and by `https://github.com.attacker.net/`,
    # so it asserts something weaker than it appears to (CodeQL: incomplete URL
    # substring sanitization, alert 34).
    #
    # It happens to be UNEXPLOITABLE here, because `_PIN_PATTERN` anchors at `^` and
    # begins with the literal `https://github\.com/`. That is exactly why it is worth
    # fixing rather than dismissing: the assertion is correct only because of something
    # OUTSIDE it. Parameterise that host one day and this line goes on passing while it
    # stops protecting anything — a guard whose correctness is on loan.
    host = urlsplit(url).netloc
    assert host == "github.com", f"pin must be hosted on github.com, got {host!r}: {url!r}"
    assert url.startswith("https://github.com/Digital-Frontier-LDA/akash-lease-core/"), (
        f"pin must point at this org's own release path: {url!r}"
    )
    # No PyPI, no arbitrary artifact stores — a host check, for the same reason.
    assert host not in {"pypi.org", "files.pythonhosted.org"}, (
        f"pin must not be a PyPI URL: {url!r}"
    )
    assert "gitlab" not in host, f"pin must not be a GitLab URL: {url!r}"


def test_pin_includes_sha256_digest(pin_line: str) -> None:
    """The wheel URL must be suffixed with a #sha256=<hex> integrity hash.

    Without the digest, a compromised release artifact can substitute the
    wheel without any client-side detection. The C5 review's
    "immutable release wheel" rule depends on this hash being present and
    correctly formatted.
    """
    match = _PIN_PATTERN.match(pin_line)
    assert match is not None, (
        f"akash-lease-core pin malformed (cannot extract sha256): {pin_line!r}"
    )
    sha = match.group("sha")
    assert len(sha) == 64, f"sha256 must be 64 hex chars, got {len(sha)}"
    assert all(c in "0123456789abcdef" for c in sha), f"sha256 must be lowercase hex, got {sha!r}"


def test_pin_url_and_path_agree_on_version(pin_line: str) -> None:
    """The version in the URL path must equal the version in the wheel filename.

    A mismatch means someone bumped the URL but forgot the wheel filename (or
    vice versa), and one of the two paths will 404 at install time.
    """
    match = _PIN_PATTERN.match(pin_line)
    assert match is not None, f"pin malformed: {pin_line!r}"
    path_version = match.group("version_path")
    wheel_version = match.group("version_wheel")
    assert path_version == wheel_version, (
        f"URL path version v{path_version} disagrees with wheel filename "
        f"version v{wheel_version}; one of them will 404 at install time"
    )


def test_pin_plan_doc_exists_and_references_issue_178() -> None:
    """The companion plan doc must exist and point at the same tracking issue.

    The plan doc is what tells a future contributor WHICH version to bump to.
    Without it, the guard tests above pass forever on a stale pin — the
    integrity check protects against typos, not against the version being
    months behind upstream.
    """
    plan = REPO_ROOT / "docs" / "C5-PIN-PLAN.md"
    assert plan.is_file(), (
        f"pin plan doc missing at {plan}; see C5 review item 2 in "
        f"Digital-Frontier-LDA/just-akash issue #178"
    )
    text = plan.read_text(encoding="utf-8")
    assert "#178" in text or "issue #178" in text, (
        "pin plan must reference just-akash tracking issue #178"
    )
    # The plan must cite the akash-lease-core issue so the cross-repo
    # dependency is visible from this repo alone.
    assert "akash-lease-core" in text
    assert "#13" in text, "pin plan must reference akash-lease-core tracking issue #13"


def test_only_one_akash_lease_core_pin_in_pyproject() -> None:
    """Exactly one akash-lease-core pin must exist in pyproject.toml.

    Two pins would either shadow each other (hatchling error) or silently
    disable one. A grep for the package name in dependencies must return a
    single match — anything else is a config drift.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    matches = [
        line
        for line in text.splitlines()
        if line.strip().startswith('"akash-lease-core') and "akash_lease_core-" in line
    ]
    assert len(matches) == 1, (
        f"expected exactly one akash-lease-core pin in {PYPROJECT}, "
        f"found {len(matches)}:\n  " + "\n  ".join(matches)
    )
