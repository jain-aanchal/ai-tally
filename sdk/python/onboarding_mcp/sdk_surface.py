# SPDX-License-Identifier: Apache-2.0
"""Grounding on the real SDK surface (CTO-261 sections 5.2, 5.3).

This is the anti-hallucination core. A recipe's ``sdk_surface.call`` is the
module-level convenience form the template emits (``tally.record_vector_call``);
``delegates_to`` is the ``TallyClient`` method it wraps. Checking only that the named
method exists would still pass a template that called a nonexistent
``tally.record_vector_call`` (section 5.2), so the guard here parses ``edit.template``,
finds every ``tally.<name>(...)`` call it actually emits, resolves each against the
live SDK, and binds the passed arguments against the real signature. A template that
calls a function that does not exist, or passes a kwarg the real signature does not
accept, fails.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
from dataclasses import dataclass

# A template hole is a bare {snake_case} token the agent fills from the call site.
# Replace holes with a placeholder name so the template parses as real Python.
_HOLE_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
_HOLE_PLACEHOLDER = "_TALLY_HOLE"

# The module the recipes' module-level convenience calls live on.
_MODULE_ROOT = "tally"


class SurfaceError(Exception):
    """A recipe references an SDK symbol or argument that does not exist. Fails CI."""


@dataclass(frozen=True)
class EmittedCall:
    """A ``tally.<name>(...)`` call found in a recipe template."""

    name: str
    n_positional: int
    kwargs: tuple[str, ...]


def render_template(template: str) -> str:
    """Substitute ``{hole}`` tokens with a placeholder so the template parses as Python."""
    return _HOLE_RE.sub(_HOLE_PLACEHOLDER, template)


def emitted_tally_calls(template: str) -> list[EmittedCall]:
    """Parse ``template`` and return every ``tally.<name>(...)`` call it emits."""
    tree = ast.parse(render_template(template))
    calls: list[EmittedCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == _MODULE_ROOT
        ):
            kwargs = tuple(kw.arg for kw in node.keywords if kw.arg is not None)
            n_positional = sum(1 for a in node.args if not isinstance(a, ast.Starred))
            calls.append(EmittedCall(func.attr, n_positional, kwargs))
    return calls


def resolve_dotted(dotted: str):
    """Resolve a dotted path (module + attribute chain) to the live object.

    Imports the longest importable module prefix, then walks the remaining attributes,
    so ``tally.client.TallyClient.record_vector_call`` resolves the method object.
    """
    parts = dotted.split(".")
    module = None
    split_at = 0
    for i in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:i]))
            split_at = i
            break
        except ImportError:
            continue
    if module is None:
        raise SurfaceError(f"no importable module in {dotted!r}")
    obj = module
    for attr in parts[split_at:]:
        try:
            obj = getattr(obj, attr)
        except AttributeError as exc:
            raise SurfaceError(f"{dotted!r} does not resolve: {exc}") from exc
    return obj


def _signature_without_self(fn) -> inspect.Signature:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name == "self":
        return sig.replace(parameters=params[1:])
    return sig


def _is_bare_var_keyword(sig: inspect.Signature) -> bool:
    """True for a ``def f(**kwargs)`` wrapper whose real signature lives on ``delegates_to``."""
    params = list(sig.parameters.values())
    return len(params) == 1 and params[0].kind is inspect.Parameter.VAR_KEYWORD


def module_symbol(name: str):
    """Return the public ``tally.<name>`` symbol, or raise if it does not exist."""
    root = importlib.import_module(_MODULE_ROOT)
    if not hasattr(root, name):
        raise SurfaceError(f"tally.{name} does not exist on the SDK surface")
    return getattr(root, name)


def validate_recipe_surface(recipe) -> None:
    """The resolve-the-call guard for one recipe. Raises :class:`SurfaceError` on any mismatch.

    Steps (section 5.2):
      1. The otel recipe emits config, not an SDK call: nothing to resolve.
      2. The declared ``call`` must be a real ``tally.<name>`` symbol.
      3. ``delegates_to`` must resolve, and every ``required_args`` must be a real
         parameter of it.
      4. Every ``tally.<name>(...)`` the template emits must resolve; the declared
         call is bound against ``delegates_to``'s real signature, so an unknown kwarg
         or a missing required arg fails.
    """
    surface = recipe.sdk_surface
    call = surface.get("call")
    delegates_to = surface.get("delegates_to")

    if call is None:
        # otel-ingest: config only, no SDK call to resolve. Guarded by schema elsewhere.
        return

    if not call.startswith(f"{_MODULE_ROOT}."):
        raise SurfaceError(f"{recipe.id}: sdk_surface.call {call!r} must be a tally.* convenience")
    call_name = call[len(_MODULE_ROOT) + 1 :]

    # (2) The module-level convenience must exist and be callable.
    conv = module_symbol(call_name)
    if not callable(conv):
        raise SurfaceError(f"{recipe.id}: {call} is not callable")

    # (3) Resolve the delegate and check the recipe's declared required_args are real.
    bind_sig: inspect.Signature | None = None
    if delegates_to:
        delegate = resolve_dotted(delegates_to)
        if not callable(delegate):
            raise SurfaceError(f"{recipe.id}: delegates_to {delegates_to} is not callable")
        bind_sig = _signature_without_self(delegate)
        for arg in surface.get("required_args", []):
            if arg not in bind_sig.parameters:
                raise SurfaceError(
                    f"{recipe.id}: required_arg {arg!r} is not a parameter of {delegates_to}"
                )

    # (4) Resolve every tally.* call the template actually emits and bind the declared one.
    emitted = emitted_tally_calls(recipe.edit["template"])
    declared = [c for c in emitted if c.name == call_name]
    if not declared:
        raise SurfaceError(
            f"{recipe.id}: template does not emit the declared call tally.{call_name}(...)"
        )

    for emitted_call in emitted:
        # Existence guard for secondary calls too (e.g. start_trace in a middleware template).
        symbol = module_symbol(emitted_call.name)
        target_sig: inspect.Signature | None
        if emitted_call.name == call_name and bind_sig is not None:
            target_sig = bind_sig
        else:
            sig = _signature_without_self(symbol)
            # A bare **kwargs wrapper carries no real signature to bind against; the
            # declared call is the one pinned via delegates_to, so skip binding here.
            target_sig = None if _is_bare_var_keyword(sig) else sig
        if target_sig is None:
            continue
        placeholder = object()
        try:
            target_sig.bind(
                *([placeholder] * emitted_call.n_positional),
                **{k: placeholder for k in emitted_call.kwargs},
            )
        except TypeError as exc:
            raise SurfaceError(
                f"{recipe.id}: tally.{emitted_call.name}(...) does not match the real "
                f"signature: {exc}"
            ) from exc


# --------------------------------------------------------------------------- #
# Layer grounding for explain_layer (section 4.2). Each entry is pinned to a real
# SDK symbol resolved at call time, never a hand-written method name.
# --------------------------------------------------------------------------- #
_LAYER_GROUNDING = {
    "llm": {
        "call": "tally.record_llm_call",
        "delegates_to": "tally.client.TallyClient.record_llm_call",
        "operation_name": "chat",
        "signal": "GenAiOperation = 'chat'",
        "why": "LLM chat completions land with gen_ai.operation.name = 'chat'.",
    },
    "tool": {
        "call": "tally.record_tool_call",
        "delegates_to": "tally.client.TallyClient.record_tool_call",
        "operation_name": "tool",
        "signal": "GenAiOperation = 'tool'",
        "why": "An app's own billable tool call is bucketed on gen_ai.operation.name = 'tool'.",
    },
    "vector": {
        "call": "tally.record_vector_call",
        "delegates_to": "tally.client.TallyClient.record_vector_call",
        "operation_name": "vector",
        "signal": "GenAiOperation = 'vector'",
        "why": "A vector-DB call is bucketed on gen_ai.operation.name = 'vector'.",
    },
    "embeddings": {
        "call": "tally.record_embedding_call",
        "delegates_to": "tally.client.TallyClient.record_embedding_call",
        "operation_name": "embeddings",
        "signal": "GenAiOperation = 'embeddings'",
        "why": "An embedding call is bucketed on gen_ai.operation.name = 'embeddings'.",
    },
    "account": {
        "call": "tally.with_account",
        "delegates_to": "tally.context.with_account",
        "operation_name": "",
        "signal": "AccountIdHash != ''",
        "why": "Per-customer attribution is set by with_account and rides as the HMAC'd "
        "AccountIdHash column, not a gen_ai.operation.name value.",
    },
}

# Some callers name the layer "embedding" (kind) vs "embeddings" (coverage). Accept both.
_LAYER_ALIASES = {"embedding": "embeddings", "chat": "llm", "llm": "llm"}


def layer_grounding(layer: str) -> dict[str, str] | None:
    """Return the SDK-grounded facts for a coverage layer, or None for an unknown layer."""
    key = _LAYER_ALIASES.get(layer, layer)
    facts = _LAYER_GROUNDING.get(key)
    if facts is None:
        return None
    # Resolve the pinned symbol now so a drifted method name would surface as an error,
    # not stale prose.
    resolve_dotted(facts["delegates_to"])
    return dict(facts, layer=key)
