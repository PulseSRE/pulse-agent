"""Verify all version references agree."""

from importlib.metadata import version


def test_api_version_matches_package():
    """The FastAPI app version should match the installed package version."""
    # No try/except: fastapi is a hard dependency, so an ImportError here means
    # the environment is broken and should fail loudly rather than skip.
    from sre_agent.api import app

    assert app.version == version("openshift-sre-agent")
