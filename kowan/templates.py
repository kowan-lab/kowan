"""App-template catalog: model, built-in loader and input substitution.

A *template* is a ready-to-deploy application blueprint. It is either a
single-container spec or a compose file, plus a list of *inputs* the UI asks
for before deploying (ports, passwords, paths). Inputs are referenced in the template body as ``${NAME}``.

Two sources feed the catalog:

* built-in templates shipped with the package in ``templates_builtin.json``
  (``builtIn=True``, never persisted, immutable);
* custom templates created by an admin, stored in the ``templates`` table
  (see :mod:`kowan.store.templates`).

Substitution rules (:func:`render`):

* Only ``${NAME}`` tokens whose NAME is a declared input (or the reserved
  :data:`RESERVED_NAME`) are replaced. Any other ``${...}`` is left untouched, so
  a compose file can still use compose's own interpolation
  (``${IMMICH_VERSION:-release}``).
* Container templates keep their spec as a plain dict until rendering, so a
  numeric field can hold a token (``"hostPort": "${HTTP_PORT}"``); after
  substitution the dict is validated into a :class:`ContainerSpec`, which
  coerces ``"8080"`` to ``8080`` (pydantic lax mode).
* In compose YAML every ``$`` in a value is escaped as ``$$`` (compose would
  otherwise interpolate it again), and values containing a newline, a double
  quote or a backslash are refused, since templates place values inside
  double-quoted YAML scalars.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from kowan.models import ContainerSpec, sanitize_resource_name
from kowan.models._serde import drop_empty

TemplateTypeContainer = "container"
TemplateTypeCompose = "compose"

# Reserved variable, always available: the Docker-safe deployment name. Used by
# container templates to give named volumes a per-deployment name
# ("${KOWAN_NAME}-data"), so two deployments of the same template on one host do
# not silently share a volume (compose namespaces volumes by project already).
RESERVED_NAME = "KOWAN_NAME"

# Input names follow environment-variable conventions.
INPUT_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class TemplateError(ValueError):
    """A template or a set of input values is invalid (maps to HTTP 400)."""


class TemplateInput(BaseModel):
    """One value the UI asks for before deploying a template."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(default="")
    label: str = Field(default="")
    description: str = Field(default="")
    default: str = Field(default="")
    required: bool = Field(default=False)
    # secret: render as a password field; with an empty default the UI
    # pre-fills a random value so the two-click deploy still works.
    secret: bool = Field(default=False)

    @model_serializer(mode="wrap")
    def _ser(self, handler):
        return drop_empty(self, handler(self), "description", "default")


class Template(BaseModel):
    """A ready-to-deploy application blueprint in the app catalog."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    name: str = Field(default="")
    description: str = Field(default="")
    category: str = Field(default="")
    icon: str = Field(default="")
    type: str = Field(default="")
    # Raw container spec (may hold ${VAR} tokens in numeric fields).
    spec: Optional[Dict[str, Any]] = Field(default=None)
    compose: str = Field(default="")
    inputs: List[TemplateInput] = Field(default_factory=list)
    # Optional project URL shown in the UI ("docs").
    website: str = Field(default="")
    built_in: bool = Field(default=False, alias="builtIn")
    created_at: str = Field(default="", alias="createdAt")
    updated_at: str = Field(default="", alias="updatedAt")

    @model_serializer(mode="wrap")
    def _ser(self, handler):
        return drop_empty(
            self, handler(self), "icon", "spec", "compose", "website",
            "created_at", "updated_at",
        )


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def validate_template(t: Template) -> None:
    """Raise :class:`TemplateError` when ``t`` is not deployable.

    Checks the required fields, the input declarations, and that the body
    renders with placeholder values into a valid spec / non-empty compose.
    """
    if t.name.strip() == "":
        raise TemplateError("name is required")
    if t.type == TemplateTypeContainer:
        if not isinstance(t.spec, dict) or str(t.spec.get("image", "")).strip() == "":
            raise TemplateError("a container template requires a spec with an image")
    elif t.type == TemplateTypeCompose:
        if t.compose.strip() == "":
            raise TemplateError("a compose template requires compose YAML")
    else:
        raise TemplateError("type must be 'container' or 'compose'")

    seen: set[str] = set()
    for inp in t.inputs:
        if not INPUT_NAME_RE.match(inp.name):
            raise TemplateError(
                f"input name {inp.name!r} must be UPPER_SNAKE_CASE (A-Z, 0-9, _)"
            )
        if inp.name == RESERVED_NAME:
            raise TemplateError(f"input name {RESERVED_NAME} is reserved")
        if inp.name in seen:
            raise TemplateError(f"duplicate input {inp.name}")
        seen.add(inp.name)

    # A dry render with placeholder values proves the body is well-formed.
    sample = {i.name: (i.default or _placeholder(i.name)) for i in t.inputs}
    render(t, sample, name="sample")


def _placeholder(name: str) -> str:
    """A value that is valid in any field: numeric for ports, text otherwise."""
    return "8080" if "PORT" in name else "placeholder"


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def resolve_values(t: Template, values: Dict[str, Any], name: str) -> Dict[str, str]:
    """Merge user ``values`` over input defaults, enforce ``required``, add the
    reserved :data:`RESERVED_NAME`. Unknown keys are ignored."""
    out: Dict[str, str] = {}
    for inp in t.inputs:
        raw = values.get(inp.name)
        v = "" if raw is None else str(raw)
        if v == "":
            v = inp.default
        if v == "" and inp.required:
            raise TemplateError(f"{inp.label or inp.name} is required")
        out[inp.name] = v
    out[RESERVED_NAME] = sanitize_resource_name(name) or "app"
    return out


def substitute(text: str, values: Dict[str, str], escape_dollar: bool = False) -> str:
    """Replace each ``${NAME}`` whose NAME is in ``values``; leave others."""

    def repl(m: "re.Match[str]") -> str:
        key = m.group(1)
        if key not in values:
            return m.group(0)
        v = values[key]
        return v.replace("$", "$$") if escape_dollar else v

    return _TOKEN_RE.sub(repl, text)


def _substitute_tree(node: Any, values: Dict[str, str]) -> Any:
    if isinstance(node, str):
        return substitute(node, values)
    if isinstance(node, list):
        return [_substitute_tree(x, values) for x in node]
    if isinstance(node, dict):
        return {k: _substitute_tree(v, values) for k, v in node.items()}
    return node


def render(t: Template, values: Dict[str, Any], name: str = "") -> Dict[str, Any]:
    """Render ``t`` with ``values`` into a deployment body fragment.

    Returns ``{"type", "spec", "compose"}`` (spec as a camelCase dict, ready
    for ``POST /deployments``). Raises :class:`TemplateError` on missing
    required inputs or an invalid result.
    """
    vals = resolve_values(t, values, name)
    if t.type == TemplateTypeContainer:
        raw = _substitute_tree(t.spec or {}, vals)
        try:
            spec = ContainerSpec.model_validate(raw)
        except Exception as err:  # noqa: BLE001 - pydantic ValidationError
            raise TemplateError(f"rendered spec is invalid: {err}") from err
        if spec.image.strip() == "":
            raise TemplateError("rendered spec has no image")
        return {
            "type": TemplateTypeContainer,
            "spec": spec.model_dump(by_alias=True),
            "compose": "",
        }
    if t.type == TemplateTypeCompose:
        for inp in t.inputs:
            v = vals.get(inp.name, "")
            if any(c in v for c in ('\n', '\r', '"', '\\')):
                raise TemplateError(
                    f"{inp.label or inp.name} must not contain quotes, backslashes or newlines"
                )
        compose = substitute(t.compose, vals, escape_dollar=True)
        _check_compose(compose)
        return {"type": TemplateTypeCompose, "spec": None, "compose": compose}
    raise TemplateError("type must be 'container' or 'compose'")


def _check_compose(compose: str) -> None:
    """Parse the rendered YAML and require a non-empty ``services`` mapping."""
    try:
        import yaml  # pyyaml is a server dependency (requirements.txt)

        from kowan import yamlsafe
    except ImportError:  # pragma: no cover - only without pyyaml
        return
    try:
        doc = yamlsafe.safe_load(compose)
    except yaml.YAMLError as err:
        raise TemplateError(f"compose YAML is invalid: {err}") from err
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
        raise TemplateError("compose YAML must define at least one service")


# ---------------------------------------------------------------------------
# built-in catalog
# ---------------------------------------------------------------------------

_BUILTIN_JSON = "templates_builtin.json"
_builtins: Optional[List[Template]] = None


def load_builtins_from(raw: str) -> List[Template]:
    """Parse a catalog JSON document; every entry is marked built-in."""
    out: List[Template] = []
    for entry in json.loads(raw):
        t = Template.model_validate(entry)
        t.built_in = True
        out.append(t)
    return out


def builtins() -> List[Template]:
    """Return a fresh copy of the built-in catalog (parsed once, cached).

    A malformed shipped catalog is a build-time bug, so parsing raises rather
    than silently returning an empty list.
    """
    global _builtins
    if _builtins is None:
        raw = resources.files("kowan").joinpath(_BUILTIN_JSON).read_text(encoding="utf-8")
        _builtins = load_builtins_from(raw)
    return [t.model_copy(deep=True) for t in _builtins]


def find_builtin(template_id: str) -> Optional[Template]:
    for t in builtins():
        if t.id == template_id:
            return t
    return None
