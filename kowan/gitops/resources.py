"""Declarative resource files: the ``apiVersion: kowan/v1`` format.

A sync's repository holds YAML files (``*.yml`` / ``*.yaml``, any depth under
the sync's path, ``---``-separated documents allowed). Every document is::

    apiVersion: kowan/v1
    kind: Deployment          # one of the registered kinds (see kinds.py)
    metadata:
      name: web               # unique per kind
      labels: {team: core}    # optional, informational
    spec: {...}               # kind-specific

Documents without ``apiVersion``/``kind`` (e.g. a plain compose file kept in
the same repo and referenced by ``composeFile``) are ignored, so a repo can mix
Kowan resources and the files they point at. Anything that *claims* to be a
Kowan resource but is malformed is a parse error, and a plan with errors is
never applied.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml

from kowan import yamlsafe

API_VERSION = "kowan/v1"
MAX_FILE_BYTES = 1 << 20
MAX_FILES = 1000


@dataclass
class Resource:
    kind: str
    name: str
    spec: Dict[str, Any]
    file: str = ""  # repo-relative path of the defining file
    doc: int = 0  # document index within the file
    labels: Dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> Tuple[str, str]:
        return (self.kind, self.name)


@dataclass
class ParseError:
    file: str
    doc: int
    message: str
    kind: str = ""
    name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"file": self.file, "doc": self.doc, "message": self.message}
        if self.kind:
            d["kind"] = self.kind
        if self.name:
            d["name"] = self.name
        return d


def parse_documents(text: str, file: str = "") -> Tuple[List[Resource], List[ParseError]]:
    """Parse one YAML file into Kowan resources (non-Kowan documents skipped)."""
    out: List[Resource] = []
    errs: List[ParseError] = []
    try:
        docs = list(yamlsafe.safe_load_all(text))
    except yaml.YAMLError as err:
        mark = getattr(err, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        return [], [ParseError(file, 0, f"invalid YAML{where}: {getattr(err, 'problem', None) or err}")]
    for i, d in enumerate(docs):
        if d is None:
            continue
        if not isinstance(d, dict) or ("apiVersion" not in d and "kind" not in d):
            continue  # not a Kowan resource (e.g. a compose file)
        api = d.get("apiVersion")
        kind = d.get("kind")
        meta = d.get("metadata")
        if api != API_VERSION:
            errs.append(ParseError(file, i, f"apiVersion must be {API_VERSION!r} (got {api!r})", str(kind or "")))
            continue
        if not isinstance(kind, str) or not kind:
            errs.append(ParseError(file, i, "kind is required"))
            continue
        if not isinstance(meta, dict) or not isinstance(meta.get("name"), str) or not meta["name"].strip():
            errs.append(ParseError(file, i, "metadata.name is required", kind))
            continue
        extra = set(d) - {"apiVersion", "kind", "metadata", "spec"}
        if extra:
            errs.append(ParseError(file, i, f"unknown top-level field(s): {', '.join(sorted(map(str, extra)))}",
                                   kind, meta["name"]))
            continue
        spec = d.get("spec") if d.get("spec") is not None else {}
        if not isinstance(spec, dict):
            errs.append(ParseError(file, i, "spec must be a mapping", kind, meta["name"]))
            continue
        labels = meta.get("labels") if isinstance(meta.get("labels"), dict) else {}
        out.append(Resource(kind=kind, name=meta["name"].strip(), spec=spec, file=file, doc=i,
                            labels={str(k): str(v) for k, v in labels.items()}))
    return out, errs


def _safe_join(root: str, rel: str) -> Optional[str]:
    root_abs = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_abs, rel))
    if target != root_abs and not target.startswith(root_abs + os.sep):
        return None
    return target


def scan_tree(checkout: str, path: str = "") -> Tuple[List[Resource], List[ParseError]]:
    """Parse every YAML file under ``checkout/path`` (hidden dirs skipped).

    Duplicate ``(kind, name)`` pairs are errors (both occurrences reported).
    """
    rel = (path or "").strip().strip("/")
    if ".." in rel.split("/"):
        return [], [ParseError(rel, 0, "sync path must stay inside the repository")]
    base = _safe_join(checkout, rel) if rel else os.path.realpath(checkout)
    if base is None:
        return [], [ParseError(rel, 0, "sync path escapes the repository")]
    if not os.path.isdir(base):
        return [], [ParseError(rel, 0, f"path {rel or '/'!r} not found in the repository")]
    resources: List[Resource] = []
    errors: List[ParseError] = []
    files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith(".") or not fn.endswith((".yml", ".yaml")):
                continue
            files.append(os.path.join(dirpath, fn))
    if len(files) > MAX_FILES:
        return [], [ParseError(rel, 0, f"too many YAML files ({len(files)} > {MAX_FILES})")]
    root_abs = os.path.realpath(checkout)
    for fp in files:
        relf = posixpath.join(*os.path.relpath(fp, root_abs).split(os.sep))
        if os.path.islink(fp) and _safe_join(checkout, relf) is None:
            errors.append(ParseError(relf, 0, "symlink escapes the repository"))
            continue
        try:
            if os.path.getsize(fp) > MAX_FILE_BYTES:
                errors.append(ParseError(relf, 0, "file is too large"))
                continue
            with open(fp, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as err:
            errors.append(ParseError(relf, 0, f"cannot read: {err}"))
            continue
        res, errs = parse_documents(text, relf)
        resources.extend(res)
        errors.extend(errs)
    seen: Dict[Tuple[str, str], Resource] = {}
    for r in resources:
        if r.key in seen:
            first = seen[r.key]
            errors.append(ParseError(r.file, r.doc, f"duplicate {r.kind} {r.name!r} (also in {first.file})", r.kind, r.name))
        else:
            seen[r.key] = r
    return resources, errors


def read_repo_file(checkout: str, base_file: str, ref: str) -> str:
    """Read ``ref`` relative to ``base_file``'s directory (or the repo root when
    it starts with ``/``), refusing anything outside the checkout."""
    if ref.startswith("/"):
        rel = ref.lstrip("/")
    else:
        rel = posixpath.normpath(posixpath.join(posixpath.dirname(base_file), ref))
    if rel.startswith("..") or rel.startswith("/"):
        raise ValueError(f"file {ref!r} is outside the repository")
    target = _safe_join(checkout, rel)
    if target is None:
        raise ValueError(f"file {ref!r} is outside the repository")
    if not os.path.isfile(target):
        raise ValueError(f"file {rel!r} not found in the repository")
    if os.path.getsize(target) > MAX_FILE_BYTES:
        raise ValueError(f"file {rel!r} is too large")
    with open(target, encoding="utf-8") as f:
        return f.read()


def render_document(kind: str, name: str, spec: Dict[str, Any], labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    meta: Dict[str, Any] = {"name": name}
    if labels:
        meta["labels"] = dict(labels)
    return {"apiVersion": API_VERSION, "kind": kind, "metadata": meta, "spec": spec}


class _LiteralDumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper: yaml.SafeDumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LiteralDumper.add_representer(str, _str_presenter)


def dump_yaml(obj: Any) -> str:
    """Readable YAML: block style, insertion order, multi-line strings as ``|``."""
    return yaml.dump(obj, Dumper=_LiteralDumper, sort_keys=False, default_flow_style=False,
                     allow_unicode=True, width=120)
