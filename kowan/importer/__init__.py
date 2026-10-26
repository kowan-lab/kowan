"""Bring existing stacks under Kowan: from a Portainer instance (its API) or
from compose folders (the files, read by the browser and posted here).

Both produce the same candidates, which the API turns into ordinary
deployments that are created, not deployed: a stack imported with the same
compose project name adopts the containers that already run.
"""
