"""MCP prompts: reusable, parameterised starting points for common jobs."""

from __future__ import annotations

from typing import Any, Dict, List

from .protocol import INVALID_PARAMS, RPCError

_PROMPTS: List[Dict[str, Any]] = [
    {
        "name": "diagnose_container",
        "title": "Diagnose a failing container",
        "description": "Investigate why a container is crashing, restarting or unhealthy and propose a fix.",
        "arguments": [
            {"name": "container", "description": "Container id or name", "required": True},
            {"name": "host", "description": 'Host id or name (default "local")', "required": False},
        ],
    },
    {
        "name": "fleet_health_report",
        "title": "Fleet health report",
        "description": "A concise health report of every host: what is down, what is degraded, what to clean up.",
        "arguments": [],
    },
    {
        "name": "update_review",
        "title": "Review available image updates",
        "description": "List containers with newer images on a host and plan a safe update order.",
        "arguments": [{"name": "host", "description": 'Host id or name (default "local")', "required": False}],
    },
]


def _text(role: str, text: str) -> Dict[str, Any]:
    return {"role": role, "content": {"type": "text", "text": text}}


class PromptCatalog:
    def list(self) -> List[Dict[str, Any]]:
        return [dict(p) for p in _PROMPTS]

    def get(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        spec = next((p for p in _PROMPTS if p["name"] == name), None)
        if spec is None:
            raise RPCError(INVALID_PARAMS, f"unknown prompt: {name}")
        for a in spec["arguments"]:
            if a.get("required") and not str(args.get(a["name"]) or "").strip():
                raise RPCError(INVALID_PARAMS, f"missing required argument: {a['name']}")
        host = str(args.get("host") or "local")
        if name == "diagnose_container":
            c = str(args["container"])
            body = (
                f"Diagnose the container `{c}` on host `{host}` managed by Kowan.\n\n"
                "1. Call inspect_container to read its state, exit code, OOM flag, health check output and restart count.\n"
                "2. Call container_logs with tail=300 (and grep for error/exception/fatal if the log is noisy).\n"
                "3. Call recent_events with type=container for the last 6h to see die/oom/health_status history.\n"
                "4. If it is running, call container_stats to check CPU and memory pressure, and workload_resources for the trend.\n"
                "5. If its workload keeps its logs, call search_logs for errors from before its last restart.\n"
                "6. If it belongs to a deployment, call get_deployment and list_revisions: did a recent change break it?\n\n"
                "Then explain the most likely root cause with the evidence, and propose the smallest fix. "
                "Do not run any mutating tool (restart, update, remove) without asking me first."
            )
        elif name == "fleet_health_report":
            body = (
                "Produce a health report of my Kowan fleet.\n\n"
                "Start with fleet_summary. For each crashed or unhealthy container, pull the last 50 log lines with "
                "container_logs to give a one-line probable cause. Include open alerts (list_alerts) and reclaimable disk "
                "per host, uptime checks that are down (uptime_status) and workloads whose memory keeps growing "
                "(memory_growth). Finish with a short prioritized action list. Read-only: do not change anything."
            )
        else:
            body = (
                f"Review image updates on host `{host}`.\n\n"
                "Call check_updates with only_available=true. Group the containers by deployment/compose project, flag "
                "databases and stateful services as higher risk, and propose an update order. Do not call "
                "update_container until I approve the plan."
            )
        return {"description": spec["description"], "messages": [_text("user", body)]}
