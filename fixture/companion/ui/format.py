"""Display helpers."""


def label(text):
    """Return text trimmed and title-cased."""
    return text.strip().title()


def percent(value):
    """Return a fraction such as 0.25 as the string '25%'."""
    return "%d%%" % round(value * 100)
