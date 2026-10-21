import os
import unittest


def symlink_or_skip(test: unittest.TestCase, link, target) -> None:
    """Make a symlink, or skip the test where that needs privileges (Windows without developer mode)."""
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError):
        test.skipTest("cannot create symlinks here")


POSIX_MODES = os.name != "nt"  # Windows has no POSIX permission bits to assert on
