"""Present so that ``tests/`` itself lands on ``sys.path``.

Shared helpers live beside the suites as plain modules (``tests/synthetic_ws.py``), and
``tests/`` is deliberately not a package. Under pytest's prepend import mode the directory
of each collected ``conftest.py`` is inserted on ``sys.path``, so a full-suite run reached
those helpers only because ``testpaths`` collects ``tests`` as a whole — and running one
subdirectory (``pytest tests/schema``) failed collection with ``ModuleNotFoundError: No
module named 'synthetic_ws'``. Targeted subdirectory runs are the normal working lane, so
they must not need a ``PYTHONPATH`` incantation to collect.

It also holds the shared BL43LXU workspace fixtures (``tests/bl43_fixture.py``), reachable from every
suite under ``tests/``.
"""
import shutil

import pytest


@pytest.fixture(scope="session")
def bl43_ws_readonly(tmp_path_factory):
    """The BL43-shaped workspace (4 analyzers per set, stamped fits), built once per xdist worker.
    Never write to it: tests that change a workspace take ``bl43_ws``."""
    from bl43_fixture import build_bl43_workspace

    return build_bl43_workspace(tmp_path_factory.mktemp("bl43"))


@pytest.fixture
def bl43_ws(bl43_ws_readonly, tmp_path):
    """A private copy of the BL43 workspace (and its ``incoming/`` data), for tests that change it."""
    copy = tmp_path / "bl43"
    shutil.copytree(bl43_ws_readonly.parent, copy)
    return copy / bl43_ws_readonly.name


@pytest.fixture(scope="session")
def scans_view_ws_readonly(tmp_path_factory):
    """The richer spectra/list workspace, generated once per xdist worker."""
    from scans_view_fixture import build_scans_view_workspace

    return build_scans_view_workspace(tmp_path_factory.mktemp("scans-view"))


@pytest.fixture
def scans_view_ws(scans_view_ws_readonly, tmp_path):
    """A private copy of the scans-view workspace and its synthetic incoming files."""
    copy = tmp_path / "scans-view"
    shutil.copytree(scans_view_ws_readonly.parent, copy)
    return copy / scans_view_ws_readonly.name
