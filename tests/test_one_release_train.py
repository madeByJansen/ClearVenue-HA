"""One release train: an add-on says the number of the ClearSignage release it carries.

ClearSignage's release workflow chooses each version once, tags the commit it built, and
hands the number to release-addon.sh. Stable and beta take it for the add-on, the screen
release inside it and the venue bundle, so one number names one commit wherever it ships;
building the same release again adds a third part to the add-on's own version only. Dev,
which builds unreleased commits, is handed its own counter's number.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(filename: str, name: str):
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_release_published_for_the_first_time_is_its_own_number():
    chooser = _load("next-image-version.py", "next_image_version")

    assert chooser.version_for_release(["20261008.01", "latest"], "20261009.02") == "20261009.02"


def test_a_rebuild_of_a_release_adds_a_third_part_and_counts_up():
    chooser = _load("next-image-version.py", "next_image_version")
    first = ["20261009.02", "20261009.02-amd64", "20261009.02-bundle"]

    assert chooser.version_for_release(first, "20261009.02") == "20261009.02.1"
    again = [*first, "20261009.02.1", "20261009.02.1-aarch64"]
    assert chooser.version_for_release(again, "20261009.02") == "20261009.02.2"


@pytest.mark.parametrize("release", ["20261009.2", "v20261009.02", "20261009.02.1", ""])
def test_a_release_that_is_not_one_is_refused(release):
    chooser = _load("next-image-version.py", "next_image_version")

    with pytest.raises(ValueError, match="not a ClearSignage release number"):
        chooser.version_for_release([], release)


def test_a_rebuild_is_a_version_home_assistant_and_the_pruner_both_order_after_it():
    pruner = _load("prune-ghcr-releases.py", "prune_ghcr_releases")

    assert pruner.version_key("20261009.02.1") > pruner.version_key("20261009.02")
    assert pruner.version_key("20261010.01") > pruner.version_key("20261009.02.1")
