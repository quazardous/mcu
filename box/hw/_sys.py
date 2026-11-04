# SPDX-License-Identifier: MIT
"""Paths under a root: "/" on the box, a fake tree in the tests."""

from pathlib import Path


def path(root, p):
    return Path(root) / p.lstrip("/")


def read(root, p, default=None):
    try:
        return path(root, p).read_text().strip()
    except OSError:
        return default
