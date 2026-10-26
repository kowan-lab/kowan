"""Exposure and HTTPS: what each host exposes, and publishing a container or
compose service on a domain through the Traefik reverse proxy discovered on
the host.

Pure parsing/classification lives in :mod:`labels`, :mod:`classify` and
:mod:`compose` (unit-tested without Docker); :mod:`proxy` and :mod:`certs` read
the running proxy; :mod:`service` orchestrates against ``Ops`` and the
deployment service. HTTP routes are in ``kowan/api/exposure.py``.
"""
