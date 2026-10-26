"""Registration of the two non-polling trigger kinds: cron and webhook.

Their firing is handled by existing
subsystems — cron by the workflow Scheduler (which reads the "cron" config
field as a cron expression) and webhook by the public hook (which matches the
server-generated "token" config field) — so they carry no Poller factory. They
are registered here purely so they appear in the catalog and the trigger editor
next to the polling types. The polling types (docker-image, git-commit)
self-register in their own modules.

The module body runs the registration when
:func:`kowan.triggers.register_builtins` imports it.
"""

from __future__ import annotations

from . import (
    FieldSpec,
    KindCron,
    KindWebhook,
    TypeCron,
    TypeWebhook,
    register,
)

register(
    TypeCron,
    KindCron,
    "Cron schedule",
    "Run the workflow on a cron schedule.",
    None,
    [FieldSpec(key="cron", label="Cron expression", required=True, placeholder="0 */6 * * *")],
)

# The HTTP methods a webhook trigger can be configured to match. "any" accepts
# every method; an empty configured value defaults to POST.
WEBHOOK_METHOD_OPTIONS = ["any", "GET", "POST", "PUT", "PATCH", "DELETE"]


def webhook_method_matches(configured: str, request_method: str) -> bool:
    """Whether a webhook trigger configured for ``configured`` should fire for an
    incoming request with HTTP method ``request_method``.

    Rules (mirrors the hook handler's gate):

      * an empty configured method defaults to POST;
      * "any" (case-insensitive) matches every method;
      * otherwise a case-insensitive equality of the two methods.

    Factored out of the handler so it is unit-testable without a live server.
    """
    want = (configured or "").strip() or "POST"
    if want.lower() == "any":
        return True
    return want.lower() == (request_method or "").strip().lower()


register(
    TypeWebhook,
    KindWebhook,
    "Webhook",
    "Run the workflow when an HTTP request hits its public URL. The body/headers/query are passed to the steps.",
    None,
    # The token is generated and managed by the server (shown as a URL in the
    # UI). The only user-entered field is which HTTP method fires the trigger;
    # "any" accepts every method, and the empty default is treated as POST.
    [
        FieldSpec(
            key="method",
            label="Method",
            placeholder="POST",
            options=WEBHOOK_METHOD_OPTIONS,
        )
    ],
)
