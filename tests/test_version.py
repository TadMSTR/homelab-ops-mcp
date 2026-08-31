"""The version is declared twice; pin them together.

Read pyproject.toml from disk rather than importlib.metadata: metadata comes
from the installed dist-info, which goes stale the moment the source is bumped
without a reinstall — so a parity test built on it can pass while the two
declarations actually disagree.
"""

import pathlib
import tomllib

import homelab_ops_mcp

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_package_version_matches_pyproject():
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        declared = tomllib.load(fh)["project"]["version"]
    assert homelab_ops_mcp.__version__ == declared


def test_changelog_documents_the_current_version():
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text()
    assert f"## [{homelab_ops_mcp.__version__}]" in changelog
