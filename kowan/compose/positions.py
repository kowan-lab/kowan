"""Parse YAML and remember where every node is.

PyYAML's ``safe_load`` returns plain Python objects and forgets positions; the
lower-level ``yaml.compose`` returns the node graph, whose nodes carry
``start_mark`` (0-based line/column). We walk that graph once and build a map
``path tuple -> (line, column)`` (1-based) so any check that knows *what* is
wrong (``services.web.ports[1]``) can also say *where*.

For a mapping entry the recorded position is the KEY (``ports:``), which is
what an editor should jump to; for a sequence item it is the item itself.
The value position of mapping entries is kept separately in ``values``.

Duplicate mapping keys - silently last-wins in PyYAML, an error for Compose -
are collected so the schema-lite checker can report them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml

from kowan import yamlsafe

from .diagnostics import PathSeg

Pos = Tuple[int, int]


@dataclass
class ParsedYaml:
    doc: Any = None
    keys: Dict[Tuple[PathSeg, ...], Pos] = field(default_factory=dict)
    values: Dict[Tuple[PathSeg, ...], Pos] = field(default_factory=dict)
    duplicates: List[Tuple[Tuple[PathSeg, ...], Pos]] = field(default_factory=list)
    error: Optional[str] = None
    error_pos: Pos = (0, 0)

    def pos(self, path) -> Pos:
        """Best known position for ``path``: the node itself, else the deepest
        known ancestor (so a missing key still points at its parent)."""
        p = tuple(path)
        while p:
            if p in self.keys:
                return self.keys[p]
            p = p[:-1]
        return (0, 0)

    def value_pos(self, path) -> Pos:
        p = tuple(path)
        return self.values.get(p) or self.pos(p)


def _mark(node: yaml.Node) -> Pos:
    m = node.start_mark
    return (m.line + 1, m.column + 1)


def _key_value(node: yaml.Node) -> Any:
    """Mapping keys as strings (Compose keys are strings; a numeric key such
    as a port is addressed by its text)."""
    if isinstance(node, yaml.ScalarNode):
        return node.value
    return None


def _walk(node: yaml.Node, path: Tuple[PathSeg, ...], out: ParsedYaml, seen: set) -> None:
    if id(node) in seen:  # aliases can make the graph cyclic-looking
        return
    seen = seen | {id(node)}
    if isinstance(node, yaml.MappingNode):
        keys_here: Dict[Any, Pos] = {}
        for knode, vnode in node.value:
            if isinstance(knode, yaml.ScalarNode) and knode.value == "<<":
                continue  # merge key: positions come from the anchor
            key = _key_value(knode)
            if key is None:
                continue
            seg: PathSeg = str(key)
            kp = path + (seg,)
            if seg in keys_here:
                out.duplicates.append((kp, _mark(knode)))
            keys_here[seg] = _mark(knode)
            out.keys[kp] = _mark(knode)
            out.values[kp] = _mark(vnode)
            _walk(vnode, kp, out, seen)
    elif isinstance(node, yaml.SequenceNode):
        for i, item in enumerate(node.value):
            ip = path + (i,)
            out.keys[ip] = _mark(item)
            out.values[ip] = _mark(item)
            _walk(item, ip, out, seen)


def parse(text: str) -> ParsedYaml:
    """Parse ``text``; on a YAML error, ``error``/``error_pos`` are set."""
    out = ParsedYaml()
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        out.doc = yamlsafe.safe_load(text)
    except yaml.MarkedYAMLError as err:
        mark = err.problem_mark or err.context_mark
        out.error = f"{err.problem or err.context or 'invalid YAML'}".strip()
        if mark is not None:
            out.error_pos = (mark.line + 1, mark.column + 1)
        return out
    except yaml.YAMLError as err:
        out.error = str(err)
        return out
    if node is not None:
        _walk(node, (), out, set())
    return out
