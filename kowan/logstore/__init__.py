"""Retained, searchable container logs.

Workloads opt in with a retention (days). The collector follows the logs of
their running containers on every host and stores the lines in a separate
SQLite file (``logs.db`` in the data dir, outside the configuration backup)
with a full-text index; the API searches it across hosts and time ranges.
"""
