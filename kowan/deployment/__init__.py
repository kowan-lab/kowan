"""Deployments: one record for a container or a compose stack, on one host.

Exposes the :class:`DeploymentService` used by
the api layer (``kowan.api.deployments``) and the webhook/log handlers.
"""

from .service import DeploymentService

__all__ = ["DeploymentService"]
