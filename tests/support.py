"""Shared scaffolding: the preferences a test's own environment describes.

Settings live in the database, and `connect` seeds that from the environment on the
first open — so a test that monkeypatches an env var before connecting is already
covered. This is for the rest: a test that sets one afterwards, or that grades rows
without a database at all, and needs the same reading handed to it explicitly.
"""
from depas.preferences import Preferences


def prefs() -> Preferences:
    """Read the environment as it stands right now, monkeypatching included."""
    return Preferences.from_env()
