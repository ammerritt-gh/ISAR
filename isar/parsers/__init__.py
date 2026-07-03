"""Parser plugins + versioned registry.

Importing this package registers the shipped plugins by name (UNIFICATION_DESIGN §2.3), so
`registry.plugin_by_name(card.policy.plugin)` resolves the card-pinned parser. `registry.detect`
works standalone (no registration needed); name-based resolution needs this wiring.
"""
from isar.parsers import registry as _registry

# Import order is safe: each plugin does `from isar.parsers import registry` (a sibling submodule
# that imports only stdlib), never back into this __init__.
from isar.parsers import ill_in8 as _ill_in8
from isar.parsers import spec_s30 as _spec_s30
from isar.parsers import spec_s30_raw as _spec_s30_raw
from isar.parsers import tavi as _tavi

for _p in (_spec_s30, _spec_s30_raw, _ill_in8, _tavi):
    _registry.register_named(_p)
# TAVI is a JSON-per-scan simulator format; `registry.detect` (header-based) does not route to it —
# it is resolved by name via the card (`plugin = "tavi"`), like the real-instrument plugins.
_registry.register(_tavi, "TAVI", "PUMA", "v1")
