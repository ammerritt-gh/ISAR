"""Shared plot-spec layer (G8): declarative figure specs + semantic style tokens, rendered by
pluggable backends (matplotlib now, pyqtgraph later). Import :mod:`isar.plotspec.spec` for the
pure, dependency-light spec objects; :mod:`isar.plotspec.backends.mpl` for the matplotlib render
(pulls matplotlib, so it is not imported here to keep ``import isar.plotspec`` light)."""
