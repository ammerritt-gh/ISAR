"""Present so that ``tests/`` itself lands on ``sys.path``.

Shared helpers live beside the suites as plain modules (``tests/synthetic_ws.py``), and
``tests/`` is deliberately not a package. Under pytest's prepend import mode the directory
of each collected ``conftest.py`` is inserted on ``sys.path``, so a full-suite run reached
those helpers only because ``testpaths`` collects ``tests`` as a whole — and running one
subdirectory (``pytest tests/schema``) failed collection with ``ModuleNotFoundError: No
module named 'synthetic_ws'``. Targeted subdirectory runs are the normal working lane, so
they must not need a ``PYTHONPATH`` incantation to collect.
"""
