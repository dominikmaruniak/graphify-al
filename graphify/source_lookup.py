# On-demand exact-source lookups for AL nodes: signature, procedure body,
# object source. The graph only stores metadata (source_file, source_location)
# -- exact source text always comes from re-parsing that one file on demand,
# not from anything cached in the index.
from __future__ import annotations

import importlib
from pathlib import Path

from graphify.extract import _AL_CONFIG, _AL_MEMBER_TYPES, _al_member_name, _al_strip_quotes

_parser = None


class SourceLookupError(Exception):
    """Raised when a node's source can't be resolved to real AL text."""


def _get_parser():
    global _parser
    if _parser is None:
        from tree_sitter import Language, Parser
        mod = importlib.import_module(_AL_CONFIG.ts_module)
        language = Language(getattr(mod, _AL_CONFIG.ts_language_fn)())
        _parser = Parser(language)
    return _parser


def _parse_line(source_location: str | None) -> int | None:
    if not source_location:
        return None
    digits = "".join(ch for ch in source_location if ch.isdigit())
    return int(digits) if digits else None


def _collect_spans(node, target_line: int, ctx: dict) -> None:
    start = node.start_point[0] + 1
    end = node.end_point[0] + 1
    if not (start <= target_line <= end):
        return
    # First match wins (outermost, since this walk is top-down), not last.
    # Some tree-sitter grammars -- confirmed for tree-sitter-al's compound
    # "procedure" rule -- give a declaration node the same `type` string as
    # one of its own descendant keyword tokens (e.g. the anonymous leaf for
    # the literal "procedure" keyword is itself typed "procedure"). An
    # unconditional overwrite here lets that inner leaf clobber the real
    # declaration node once recursion reaches it, so get_signature/
    # get_procedure_body ends up extracting from that leaf's tiny span
    # instead of the actual declaration -- reproduced live as both
    # returning the bare string "procedure" for every AL procedure.
    if node.type in _AL_CONFIG.class_types and ctx.get("object") is None:
        ctx["object"] = node
    if node.type in _AL_CONFIG.function_types and ctx.get("function") is None:
        ctx["function"] = node
    for child in node.children:
        _collect_spans(child, target_line, ctx)


def _header_text(node, source: bytes) -> str:
    body = node.child_by_field_name(_AL_CONFIG.body_field)
    end = body.start_byte if body is not None else node.end_byte
    # `body` only anchors the begin/end block itself -- a var section
    # (local variable declarations) sits between the signature and the
    # body but has no field name of its own (confirmed via tree-sitter-al's
    # own field mapping: "var_section" is an unnamed/positional child), so
    # without this it's silently included as part of "the header" too --
    # reproduced live: get_signature returning the full var section
    # trailing after the real signature line. Var declarations aren't part
    # of the signature; cut there instead when a var section precedes body.
    var_section = next((c for c in node.children if c.type == "var_section"), None)
    if var_section is not None and var_section.start_byte < end:
        end = var_section.start_byte
    return source[node.start_byte:end].decode("utf-8", errors="replace").rstrip()


def _full_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _decl_name(node, source: bytes) -> str | None:
    name = node.child_by_field_name(_AL_CONFIG.name_field)
    if name is None:
        name = next((c for c in node.children
                     if c.type in _AL_CONFIG.name_fallback_child_types), None)
    if name is None:
        return None
    return _al_strip_quotes(source[name.start_byte:name.end_byte].decode("utf-8", errors="replace"))


def _qualified_name(node, source: bytes) -> str | None:
    """`Member.Name` path of a declaration inside its object: `InitQty` for a
    procedure, `Quantity.OnValidate` for a field trigger. The object itself is
    left out -- a source file holds one object."""
    own = _decl_name(node, source)
    if own is None:
        return None
    parts = [own]
    anc = node.parent
    while anc is not None and anc.type not in _AL_CONFIG.class_types:
        if anc.type in _AL_MEMBER_TYPES:
            member = _al_member_name(anc, source)
            if member:
                parts.append(_al_strip_quotes(member))
        anc = anc.parent
    return ".".join(reversed(parts))


def _names_match(actual: str | None, expected: str) -> bool:
    # Whole-path match only: a bare `OnValidate` must not pick one of a table's
    # many field triggers.
    return actual is not None and actual.lower() == expected.lower()


def _enclosing_object(node):
    anc = node.parent
    while anc is not None and anc.type not in _AL_CONFIG.class_types:
        anc = anc.parent
    return anc


def _find_declarations(node, source: bytes, expected: str, out: list) -> None:
    if node.type in _AL_CONFIG.function_types:
        if _names_match(_qualified_name(node, source), expected):
            out.append(node)
        return
    for child in node.children:
        _find_declarations(child, source, expected, out)


def _resolve_spans(source_path: Path, source_location: str | None,
                   expected_name: str | None = None) -> dict:
    """Locate the declaration at `source_location`.

    With `expected_name` (the declaration's `Member.Name` path, e.g.
    `Quantity.OnValidate`), the match is verified: when the file drifted since
    the graph was built and the stored line now sits in another declaration, the
    file is searched for that name instead. One match is used; none or several
    raise a "stale" SourceLookupError -- never another symbol's text."""
    line = _parse_line(source_location)
    if line is None and expected_name is None:
        raise SourceLookupError("No source location recorded for this node.")
    if not source_path.is_file():
        raise SourceLookupError(f"Source file not found: {source_path}")
    source = source_path.read_bytes()
    tree = _get_parser().parse(source)
    ctx: dict = {"object": None, "function": None, "source": source, "moved": False}
    if line is not None:
        _collect_spans(tree.root_node, line, ctx)
    if expected_name is None:
        return ctx
    current = ctx["function"]
    if current is not None and _names_match(_qualified_name(current, source), expected_name):
        return ctx
    if current is None and ctx["object"] is not None and \
            _names_match(_decl_name(ctx["object"], source), expected_name):
        return ctx
    found: list = []
    _find_declarations(tree.root_node, source, expected_name, found)
    if len(found) == 1:
        ctx["function"] = found[0]
        ctx["object"] = _enclosing_object(found[0])
        ctx["moved"] = True
        return ctx
    where = (f" (candidates at lines {', '.join(str(n.start_point[0] + 1) for n in found)})"
             if found else "")
    raise SourceLookupError(
        f"Graph anchor is stale: '{expected_name}' is no longer at {source_location}"
        f" in {source_path.name} and could not be relocated unambiguously{where}."
        " Rebuild the graph for this file."
    )


def get_signature(source_path: Path, source_location: str | None,
                  expected_name: str | None = None) -> str:
    spans = _resolve_spans(source_path, source_location, expected_name)
    node = spans["function"] or spans["object"]
    if node is None:
        raise SourceLookupError("No object or procedure declaration found at that location.")
    return _header_text(node, spans["source"])


def get_procedure_body(source_path: Path, source_location: str | None,
                       expected_name: str | None = None) -> str:
    spans = _resolve_spans(source_path, source_location, expected_name)
    if spans["function"] is None:
        raise SourceLookupError(
            "This node isn't inside a procedure/trigger -- use get_object_source instead."
        )
    return _full_text(spans["function"], spans["source"])


def get_object_source(source_path: Path, source_location: str | None,
                      expected_name: str | None = None) -> str:
    spans = _resolve_spans(source_path, source_location, expected_name)
    if spans["object"] is not None:
        return _full_text(spans["object"], spans["source"])
    # source_location sits above any declaration (e.g. the file-level node) --
    # fall back to the whole file, which is what a W1-28 .al file always is.
    return spans["source"].decode("utf-8", errors="replace")
