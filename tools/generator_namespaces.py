"""Read-traced mapping exposed through a read-only generator class namespace."""

from collections.abc import Mapping


class ClassNamespace(Mapping):
    def __init__(self, source, unpack, report):
        self.source = source
        self.unpack = unpack
        self.report = report

    def __getitem__(self, key):
        value = self.source[key]
        self.report(key)
        return self.unpack(value)

    def __iter__(self):
        # Key membership, order, and cardinality are identical for a same-type
        # replacement of an existing primitive class setting.
        return iter(self.source)

    def __len__(self):
        return len(self.source)

    def __contains__(self, key):
        return key in self.source

    def __repr__(self):
        return repr(dict(self.items()))

    def copy(self):
        return dict(self.items())
