"""Numpy-aware JSON codec and array-aware equality for the schema dataclasses (D1, §5.7).

The canonical store is JSON with arrays inline, but `dataclasses.asdict` leaves `np.ndarray`
in place and `json.dumps` rejects it. This module provides:

- `to_jsonable(obj)`  — recurse a dataclass tree, turning `np.ndarray`→list, np scalar→python.
- `from_dataclass(cls, d)` — rebuild a dataclass tree from the JSON dict, restoring arrays to
  `np.ndarray` of the dtype each dataclass declares in its `_ARRAY_FIELDS` class attribute.
- `objects_close(a, b)` — deep equality that uses `np.allclose` on float arrays and
  `np.array_equal` on bool arrays, because dataclass `==` cannot compare records (an
  `ndarray == ndarray` returns an array, whose truth value is ambiguous).

`_ARRAY_FIELDS` maps an array field name → numpy dtype string (e.g. `{"mask": "bool"}`); it is an
unannotated class attribute so `dataclasses.fields()` and `typing.get_type_hints()` both ignore it.
"""
from __future__ import annotations

import math
import types
from dataclasses import MISSING, fields, is_dataclass
from typing import Union, get_args, get_origin, get_type_hints

import numpy as np


def _array_fields(cls) -> dict:
    return getattr(cls, "_ARRAY_FIELDS", {})


def _is_union(origin) -> bool:
    return origin is Union or origin is getattr(types, "UnionType", ())


# ---------------------------------------------------------------------------
# Serialization: dataclass tree -> JSON-able tree
# ---------------------------------------------------------------------------
def to_jsonable(obj):
    if obj is None:
        return None
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Deserialization: JSON-able tree -> dataclass tree
# ---------------------------------------------------------------------------
def from_dataclass(cls, d: dict):
    hints = get_type_hints(cls)
    arr = _array_fields(cls)
    kwargs = {}
    for f in fields(cls):
        if f.name not in d:
            # Absent key: let the constructor apply the field's default (TOML may omit optionals).
            if f.default is not MISSING or f.default_factory is not MISSING:
                continue
            raise KeyError(f"missing required field {cls.__name__}.{f.name}")
        v = d[f.name]
        if f.name in arr:
            kwargs[f.name] = None if v is None else np.asarray(v, dtype=arr[f.name])
        else:
            kwargs[f.name] = _reconstruct(hints[f.name], v)
    return cls(**kwargs)


def _reconstruct(tp, val):
    if val is None:
        return None
    origin = get_origin(tp)
    if origin is None:
        if is_dataclass(tp):
            return from_dataclass(tp, val)
        return val
    if _is_union(origin):
        for arg in get_args(tp):
            if arg is type(None):
                continue
            return _reconstruct(arg, val)
        return val
    if origin in (list, tuple):
        args = get_args(tp)
        return [_reconstruct(args[0], v) for v in val] if args else list(val)
    if origin is dict:
        args = get_args(tp)
        return {k: _reconstruct(args[1], v) for k, v in val.items()} if len(args) == 2 else dict(val)
    return val


# ---------------------------------------------------------------------------
# Array-aware deep equality
# ---------------------------------------------------------------------------
def objects_close(a, b, *, rtol: float = 1e-7, atol: float = 1e-9) -> bool:
    return _close(a, b, rtol, atol)


def _arrays_close(a, b, dtype, rtol, atol) -> bool:
    if a is None or b is None:            # optional array fields (e.g. miniscan ensemble): None==None
        return a is None and b is None
    a, b = np.asarray(a), np.asarray(b)
    if a.shape != b.shape:
        return False
    if dtype == "bool" or a.dtype == bool or b.dtype == bool:
        return np.array_equal(a, b)
    return bool(np.allclose(a, b, rtol=rtol, atol=atol, equal_nan=True))


def _close(a, b, rtol, atol) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if is_dataclass(a) and not isinstance(a, type):
        if type(a) is not type(b):
            return False
        arr = _array_fields(type(a))
        for f in fields(a):
            va, vb = getattr(a, f.name), getattr(b, f.name)
            ok = _arrays_close(va, vb, arr[f.name], rtol, atol) if f.name in arr else _close(va, vb, rtol, atol)
            if not ok:
                return False
        return True
    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        return _arrays_close(a, b, None, rtol, atol)
    if isinstance(a, dict):
        return isinstance(b, dict) and set(a) == set(b) and all(_close(a[k], b[k], rtol, atol) for k in a)
    if isinstance(a, (list, tuple)):
        return isinstance(b, (list, tuple)) and len(a) == len(b) and all(_close(x, y, rtol, atol) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, float) or isinstance(b, float):
        af, bf = float(a), float(b)
        if math.isnan(af) and math.isnan(bf):
            return True
        return math.isclose(af, bf, rel_tol=rtol, abs_tol=atol)
    return a == b
