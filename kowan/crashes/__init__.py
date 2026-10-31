"""Crash snapshots (see ``watcher``)."""

from .watcher import CrashWatcher, redact_env

__all__ = ["CrashWatcher", "redact_env"]
