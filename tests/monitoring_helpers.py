"""Shared helpers for the monitoring tests."""


class Clock:
    """Settable clock shared by a recorder, a store and an evaluator so tests control time."""

    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def counter(snapshot, name, **labels):
    return sum(c["value"] for c in snapshot["counters"] if c["name"] == name
               and all(c["labels"].get(k) == v for k, v in labels.items()))
