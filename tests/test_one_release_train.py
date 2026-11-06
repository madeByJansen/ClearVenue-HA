"""One release train: an add-on says the number of the ClearSignage release it carries.

ClearSignage's release workflow passes the signed screen release number to the add-on.
A rebuild can publish a new dated add-on version using that same signed release.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DAY = dt.date(2026, 10, 9)


def _load(filename: str, name: str):
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_release_published_for_the_first_time_is_its_own_number():
    chooser = _load("next-image-version.py", "next_image_version")

    assert chooser.version_for_release(["20261008.01", "latest"], "20261009.02", DAY) == "20261009.02"


def test_a_rebuild_uses_the_next_dated_version_even_with_a_legacy_tag():
    chooser = _load("next-image-version.py", "next_image_version")

    assert chooser.version_for_release(["20261009.02.1-amd64"], "20261009.02", DAY) == "20261009.03"
    assert chooser.version_for_release(["20261009.03"], "20261009.02", DAY) == "20261009.04"
    assert chooser.version_for_release(
        ["20261009.02.1"], "20261009.02", dt.date(2026, 10, 10)
    ) == "20261010.01"

    with pytest.raises(ValueError, match="not an add-on version"):
        chooser.stamp_version('version: "20261009.02"\n', "20261009.02.1")


@pytest.mark.parametrize("release", ["20261009.2", "v20261009.02", "20261009.02.1", ""])
def test_a_release_that_is_not_one_is_refused(release):
    chooser = _load("next-image-version.py", "next_image_version")

    with pytest.raises(ValueError, match="not a ClearSignage release number"):
        chooser.version_for_release([], release, DAY)


def test_a_rebuild_is_a_version_home_assistant_and_the_pruner_both_order_after_it():
    pruner = _load("prune-ghcr-releases.py", "prune_ghcr_releases")

    assert pruner.version_key("20261009.02.1") > pruner.version_key("20261009.02")
    assert pruner.version_key("20261010.01") > pruner.version_key("20261009.02.1")
