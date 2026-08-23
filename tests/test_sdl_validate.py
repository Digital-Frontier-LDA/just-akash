"""Tests for just_akash.sdl_validate — SDL validation rules."""

import pytest

from just_akash.sdl_validate import (
    AUDIT_AUTHORITY_ADDRESS,
    SDLValidationError,
    validate_sdl,
)

GOOD = AUDIT_AUTHORITY_ADDRESS
BAD = "akash1baadbaadbaadbaadbaadbaadbaadbaadbaadbaad"


def _sdl(placement_block: str) -> str:
    return f"""
version: "2.0"
services:
  web:
    image: nginx
    expose:
      - port: 80
        as: 80
        to:
          - global: true
profiles:
  compute:
    web:
      resources:
        cpu:
          units: 1
        memory:
          size: 512Mi
        storage:
          - size: 1Gi
  placement:
{placement_block}
deployment:
  web:
    akash:
      profile: web
      count: 1
"""


def test_no_signed_by_passes():
    sdl = _sdl(
        """    akash:
      pricing:
        web:
          denom: uakt
          amount: 1000
"""
    )
    validate_sdl(sdl)


def test_correct_any_of_passes():
    sdl = _sdl(
        f"""    akash:
      signedBy:
        anyOf:
          - {GOOD}
      pricing:
        web:
          denom: uakt
          amount: 1000
"""
    )
    validate_sdl(sdl)


def test_correct_all_of_passes():
    sdl = _sdl(
        f"""    akash:
      signedBy:
        allOf:
          - {GOOD}
      pricing:
        web:
          denom: uakt
          amount: 1000
"""
    )
    validate_sdl(sdl)


def test_wrong_address_fails():
    sdl = _sdl(
        f"""    akash:
      signedBy:
        anyOf:
          - {BAD}
      pricing:
        web:
          denom: uakt
          amount: 1000
"""
    )
    with pytest.raises(SDLValidationError) as exc:
        validate_sdl(sdl)
    assert BAD in str(exc.value)
    assert GOOD in str(exc.value)


def test_mixed_list_one_bad_fails():
    sdl = _sdl(
        f"""    akash:
      signedBy:
        anyOf:
          - {GOOD}
          - {BAD}
      pricing:
        web:
          denom: uakt
          amount: 1000
"""
    )
    with pytest.raises(SDLValidationError) as exc:
        validate_sdl(sdl)
    assert BAD in str(exc.value)


def test_empty_any_of_fails():
    sdl = _sdl(
        """    akash:
      signedBy:
        anyOf: []
      pricing:
        web:
          denom: uakt
          amount: 1000
"""
    )
    with pytest.raises(SDLValidationError, match="non-empty list"):
        validate_sdl(sdl)


def test_signed_by_without_any_or_all_of_fails():
    sdl = _sdl(
        """    akash:
      signedBy:
        unknown: foo
      pricing:
        web:
          denom: uakt
          amount: 1000
"""
    )
    with pytest.raises(SDLValidationError, match="must contain 'anyOf' or 'allOf'"):
        validate_sdl(sdl)


def test_signed_by_not_a_mapping_fails():
    sdl = _sdl(
        f"""    akash:
      signedBy: {GOOD}
      pricing:
        web:
          denom: uakt
          amount: 1000
"""
    )
    with pytest.raises(SDLValidationError, match="must be a mapping"):
        validate_sdl(sdl)


def test_multiple_placements_all_checked():
    sdl = _sdl(
        f"""    akash:
      signedBy:
        anyOf:
          - {GOOD}
      pricing:
        web:
          denom: uakt
          amount: 1000
    dcloud:
      signedBy:
        anyOf:
          - {BAD}
      pricing:
        web:
          denom: uakt
          amount: 1000
"""
    )
    with pytest.raises(SDLValidationError) as exc:
        validate_sdl(sdl)
    msg = str(exc.value)
    assert "dcloud" in msg
    assert BAD in msg


def test_flow_style_caught():
    # Flow-style YAML — line-based parsing would miss this; PyYAML catches it.
    sdl = _sdl(
        f"""    akash:
      signedBy: {{anyOf: [{BAD}]}}
      pricing:
        web:
          denom: uakt
          amount: 1000
"""
    )
    with pytest.raises(SDLValidationError) as exc:
        validate_sdl(sdl)
    assert BAD in str(exc.value)


def test_invalid_yaml_fails():
    with pytest.raises(SDLValidationError, match="not valid YAML"):
        validate_sdl("version: '2.0'\n  bad: indentation\n :::: nope")


def test_root_not_mapping_fails():
    with pytest.raises(SDLValidationError, match="must be a YAML mapping"):
        validate_sdl("- just\n- a\n- list")


def test_repo_sdl_passes():
    """The shipped sdl/cpu-backtest-ssh.yaml has no signedBy, so it should pass."""
    from pathlib import Path

    sdl = Path(__file__).resolve().parent.parent / "sdl" / "cpu-backtest-ssh.yaml"
    validate_sdl(sdl.read_text())


def test_all_shipped_sdl_files_declare_bid_window():
    """C5 review item 1 (issue #178): every shipped SDL documents the
    bounded bid window.

    The Akash SDL spec has no native `bid_timeout` field. The bounded window
    is enforced client-side by `just-akash deploy --bid-wait` (default 60s)
    and pinned by `AuctionPolicy.collection_window_seconds` in the shared
    `akash-lease-core` package. To make the contract visible at the SDL
    boundary (so a future SDL that drifts to the wrong window is caught at
    validation rather than at runtime), every shipped SDL carries a
    `# just-akash:` annotation block that names the window. This test walks
    every `sdl/*.yaml` and fails if any one is missing the annotation.

    The annotation lives in the header comment block (above `version: "2.0"`),
    not in a structured SDL field, so this is a text-level check — keep it
    cheap and grep-friendly.
    """
    from pathlib import Path

    sdl_dir = Path(__file__).resolve().parent.parent / "sdl"
    sdl_files = sorted(p for p in sdl_dir.glob("*.yaml") if p.is_file())
    assert sdl_files, f"no SDL files found in {sdl_dir}"

    missing: list[str] = []
    for sdl_path in sdl_files:
        text = sdl_path.read_text(encoding="utf-8")
        if "# just-akash:" not in text or "bid-window" not in text:
            missing.append(sdl_path.name)

    assert not missing, (
        f"shipped SDLs missing the `# just-akash: bid-window = ...` "
        f"annotation: {missing}. Add the header comment per C5 review "
        f"issue #178 (just-akash)."
    )


def test_bid_window_annotation_references_collection_window_contract():
    """The annotation block cites the live contract, not a stale string.

    Two anchoring facts must be present together so a future edit that
    drifts the deploy-side window from 60s and forgets the SDL is caught:
    (a) `--bid-wait` and the default 60s, and (b) the shared core's
    `AuctionPolicy.collection_window_seconds` reference. Both names appear
    in the verbatim annotation block added to each SDL header — searching
    for the SUBSTRINGS catches a deleted token without parsing the YAML.
    """
    from pathlib import Path

    sdl_dir = Path(__file__).resolve().parent.parent / "sdl"
    for sdl_path in sorted(sdl_dir.glob("*.yaml")):
        text = sdl_path.read_text(encoding="utf-8")
        assert "# just-akash:" in text, f"{sdl_path.name} missing `# just-akash:` tag"
        assert "bid-window" in text, f"{sdl_path.name} missing `bid-window` key"
        assert "--bid-wait" in text, (
            f"{sdl_path.name} annotation does not cite `--bid-wait`; "
            f"if the deploy CLI flag renamed, update the annotation in lockstep"
        )
        assert "60s" in text, (
            f"{sdl_path.name} annotation does not cite the 60s default; "
            f"if the default changed, update the annotation in lockstep"
        )
        assert "AuctionPolicy" in text, (
            f"{sdl_path.name} annotation does not cite the shared "
            f"AuctionPolicy; if the core renamed, update the annotation"
        )
        assert "collection_window_seconds" in text, (
            f"{sdl_path.name} annotation does not cite "
            f"`collection_window_seconds`; if the core renamed, update "
            f"the annotation"
        )
        assert "akash-lease-core" in text, (
            f"{sdl_path.name} annotation does not cite the `akash-lease-core` "
            f"package; if the pin renamed, update the annotation"
        )
