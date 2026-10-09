"""Ensure add-on tests never inherit ClearSignage test requirements.

The release job checks out this repo inside ClearSignage/.clearvenue-ha.
Without a local pytest.ini, pytest walks to the parent pyproject.toml
and demands plugins not installed in the add-on release environment.
"""

from pathlib import Path


def test_packaging_uses_its_own_pytest_configuration(pytestconfig):
    assert pytestconfig.inipath == Path(__file__).resolve().parents[1] / "pytest.ini"
