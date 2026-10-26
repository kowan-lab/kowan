"""YAML loading that cannot be turned into a memory bomb.

``yaml.safe_load`` never runs code, but anchors and aliases are shared nodes:
a few lines (the "billion laughs") become billions of nodes as soon as the
result is walked, serialized to JSON or re-emitted. Anchors are normal in
compose files (x-env: &env …), so they stay allowed; what is bounded is the
size of the document once every alias is expanded.
"""

from __future__ import annotations

from typing import Any, List

import yaml

MAX_NODES = 100_000


class YAMLTooLarge(yaml.YAMLError):
    pass


def _check(obj: Any, limit: int) -> None:
    # Walk the expanded tree, stopping as soon as the budget is spent, so the
    # check itself costs at most ``limit`` steps whatever the document holds.
    budget = limit
    stack: List[Any] = [obj]
    while stack:
        node = stack.pop()
        budget -= 1
        if budget < 0:
            raise YAMLTooLarge(f"YAML document expands to more than {limit} nodes")
        if isinstance(node, dict):
            stack.extend(node.keys())
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)


def safe_load(text: str, max_nodes: int = MAX_NODES) -> Any:
    doc = yaml.safe_load(text)
    _check(doc, max_nodes)
    return doc


def safe_load_all(text: str, max_nodes: int = MAX_NODES) -> List[Any]:
    docs = list(yaml.safe_load_all(text))
    _check(docs, max_nodes)
    return docs
