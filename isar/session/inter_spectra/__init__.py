"""Inter-spectra fitting (the second pass): spectrum nodes, the link graph, rings and groups, the store's
records and the authority resolver, and the batch runner.

Keep this package light: no numerics and no Qt (AGENTS.md hard rule 6). The read facade imports the
pure modules here, never ``runner``.
"""
