"""Uptime checks: HTTP and TCP probes run by the control plane on a schedule,
with response times, incidents, alerts and an optional public status page.

``probe`` holds the network side (one check, one result); ``runner`` schedules
probes, keeps the per-check state and raises or resolves the alerts.
"""
