"""Core arithmetic helpers."""


def add(a, b):
    """Return a + b."""
    return a + b


def sub(a, b):
    """Return a - b."""
    return a - b


def mul(a, b):
    """Return a * b."""
    return a * b


def div(a, b):
    """Return a / b. Raise ValueError when b is zero."""
    if b == 0:
        raise ValueError("division by zero")
    return a / b


def mean(values):
    """Return the arithmetic mean. Raise ValueError for an empty sequence."""
    values = list(values)
    if not values:
        raise ValueError("mean of empty sequence")
    return sum(values) / len(values)
