"""kowan.cli — the kowanctl command-line client.

``kowanctl`` is the command-line client for the Kowan Docker management
platform. It talks to a Kowan server over its REST API, reading configuration
from flags, the environment, and ``~/.kowan/config.yaml``, in that order.

The typer app is assembled in :mod:`kowan.cli.main`; run it with ``python3 -m kowan.cli``.
"""

from kowan import __version__

from .main import app, main

__all__ = ["app", "main"]

# version is the kowanctl build version.
version = __version__
