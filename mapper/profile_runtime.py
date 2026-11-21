"""Instrumentation used only to build the coverage index on the unchanged source."""

import functools
import operator

events = set()
comparators = {
    "Lt": operator.lt,
    "LtE": operator.le,
    "Gt": operator.gt,
    "GtE": operator.ge,
    "Eq": operator.eq,
    "NotEq": operator.ne,
    "Is": operator.is_,
    "IsNot": operator.is_not,
}


def compare(site, op, a, b):
    result = comparators[op](a, b)
    # Record operand ordering only for primitive types whose comparisons agree
    # with Python's numeric/string ordering. Preserve exceptions and result type.
    if type(a) is type(b) and type(a) in (int, bool, str, bytes):
        relation = -1 if a < b else int(a > b)
    elif (
        isinstance(a, tuple)
        and type(b) is tuple
        and all(type(v) is int for v in b)
        and all(type(v) is int for v in a)
        and type(a).__lt__ is tuple.__lt__
        and type(a).__le__ is tuple.__le__
        and type(a).__gt__ is tuple.__gt__
        and type(a).__ge__ is tuple.__ge__
        and type(a).__eq__ is tuple.__eq__
        and type(a).__ne__ is tuple.__ne__
    ):
        relation = -1 if a < b else int(a > b)
    else:
        relation = 2  # overloaded comparison or unsupported operands
    events.add((site, relation))
    return result


def called(site, fn):
    @functools.wraps(fn)
    def observed(*args, **kwargs):
        events.add((site, "called"))
        return fn(*args, **kwargs)

    return observed


class ObservedDict(dict):
    def __init__(self, site, data):
        super().__init__(data)
        self.site = site

    def __getitem__(self, key):
        events.add((self.site, key))
        return super().__getitem__(key)

    def get(self, key, default=None):
        events.add((self.site, key))
        return super().get(key, default)

    def _bulk(self):
        events.add((self.site, None))

    def items(self):
        self._bulk()
        return super().items()

    def values(self):
        self._bulk()
        return super().values()

    def copy(self):
        self._bulk()
        return super().copy()

    def __iter__(self):
        # Python may use the mapping protocol for dict expansion and updates.
        self._bulk()
        return super().__iter__()

    def keys(self):
        self._bulk()
        return super().keys()

    def __repr__(self):
        self._bulk()
        return super().__repr__()

    def __eq__(self, other):
        self._bulk()
        return super().__eq__(other)

    def __ne__(self, other):
        self._bulk()
        return super().__ne__(other)

    def __or__(self, other):
        self._bulk()
        return super().__or__(other)

    def __ror__(self, other):
        self._bulk()
        return super().__ror__(other)

    def pop(self, key, *default):
        events.add((self.site, key))
        return super().pop(key, *default)

    def setdefault(self, key, default=None):
        events.add((self.site, key))
        return super().setdefault(key, default)

    def popitem(self):
        self._bulk()
        return super().popitem()
