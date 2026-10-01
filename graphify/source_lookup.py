# On-demand exact-source lookups for AL nodes: signature, procedure body,
# object source. The graph only stores metadata (source_file, source_location)
# -- exact source text always comes from re-parsing that one file on demand,
# not from anything cached in the index.
from __future__ import annotations

import importlib
import re
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
    `Quantity.OnValidate`), the file is searched for every declaration with that
    path instead of trusting the stored line, which goes stale as soon as the
    file changes after the graph was built. `group` holds all of them: AL
    overloads share one graph node id, so the node stands for the whole
    overload group. No match raises a "stale" SourceLookupError -- never another
    symbol's text."""
    line = _parse_line(source_location)
    if line is None and expected_name is None:
        raise SourceLookupError("No source location recorded for this node.")
    if not source_path.is_file():
        raise SourceLookupError(f"Source file not found: {source_path}")
    source = source_path.read_bytes()
    tree = _get_parser().parse(source)
    ctx: dict = {"object": None, "function": None, "source": source, "moved": False, "group": []}
    if line is not None:
        _collect_spans(tree.root_node, line, ctx)
    if expected_name is None:
        return ctx
    found: list = []
    _find_declarations(tree.root_node, source, expected_name, found)
    if found:
        ctx["group"] = found
        current = ctx["function"]
        if current is None or all(n.start_byte != current.start_byte for n in found):
            ctx["function"] = found[0]
            ctx["object"] = _enclosing_object(found[0])
            ctx["moved"] = True
        return ctx
    if ctx["function"] is None and ctx["object"] is not None and \
            _names_match(_decl_name(ctx["object"], source), expected_name):
        return ctx
    raise SourceLookupError(
        f"Graph anchor is stale: '{expected_name}' is no longer in {source_path.name}"
        f" (graph location {source_location}). Rebuild the graph for this file."
    )


def _group_note(spans: dict, expected_name: str | None) -> str:
    group = spans["group"]
    lines = ", ".join(f"L{n.start_point[0] + 1}" for n in group)
    return f"// {len(group)} overloads of {expected_name}: {lines}\n"


def get_signature(source_path: Path, source_location: str | None,
                  expected_name: str | None = None) -> str:
    spans = _resolve_spans(source_path, source_location, expected_name)
    if len(spans["group"]) > 1:
        return "\n".join(_header_text(n, spans["source"]) for n in spans["group"])
    node = spans["function"] or spans["object"]
    if node is None:
        raise SourceLookupError("No object or procedure declaration found at that location.")
    return _header_text(node, spans["source"])


def _lines(node) -> tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def get_procedure_body_located(source_path: Path, source_location: str | None,
                               expected_name: str | None = None
                               ) -> tuple[str, list[tuple[int, int]]]:
    """get_procedure_body plus the 1-based (start, end) line range of each
    declaration returned, so a caller can Read/Edit exactly those lines."""
    spans = _resolve_spans(source_path, source_location, expected_name)
    if len(spans["group"]) > 1:
        text = _group_note(spans, expected_name) + "\n\n".join(
            _full_text(n, spans["source"]) for n in spans["group"])
        return text, [_lines(n) for n in spans["group"]]
    if spans["function"] is None:
        raise SourceLookupError(
            "This node isn't inside a procedure/trigger -- use get_object_source instead."
        )
    return _full_text(spans["function"], spans["source"]), [_lines(spans["function"])]


def get_procedure_body(source_path: Path, source_location: str | None,
                       expected_name: str | None = None) -> str:
    return get_procedure_body_located(source_path, source_location, expected_name)[0]


def get_object_source(source_path: Path, source_location: str | None,
                      expected_name: str | None = None) -> str:
    spans = _resolve_spans(source_path, source_location, expected_name)
    if spans["object"] is not None:
        return _full_text(spans["object"], spans["source"])
    # source_location sits above any declaration (e.g. the file-level node) --
    # fall back to the whole file, which is what a W1-28 .al file always is.
    return spans["source"].decode("utf-8", errors="replace")


# ── outline: a table of contents of one AL object, with line ranges ────────────

_OUTLINE_SECTIONS = ("summary", "procedures", "triggers", "fields", "events", "all")


def _one_line(text: str, limit: int = 160) -> str:
    s = " ".join(text.split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _publisher_kind(node, source: bytes) -> str | None:
    sib = node.prev_named_sibling
    while sib is not None and sib.type == "attribute_item":
        low = source[sib.start_byte:sib.end_byte].decode("utf-8", errors="replace").lower()
        if "integrationevent" in low:
            return "integration"
        if "businessevent" in low:
            return "business"
        sib = sib.prev_named_sibling
    return None


def _outline_entries(obj, source: bytes) -> tuple[list[dict], list[dict]]:
    """(declarations, fields) of one object, in file order."""
    decls: list[dict] = []
    fields: list[dict] = []

    def walk(node, field: dict | None) -> None:
        if node.type in _AL_CONFIG.function_types:
            start, end = _lines(node)
            decls.append({
                "name": _qualified_name(node, source) or "?",
                "kind": "trigger" if node.type == "trigger_declaration" else "procedure",
                "member_trigger": field is not None,
                "event": _publisher_kind(node, source),
                "sig": _one_line(_header_text(node, source)),
                "start": start, "end": end,
            })
            if field is not None:
                field["triggers"].append(f"{decls[-1]['name'].rsplit('.', 1)[-1]} L{start}-{end}")
            return
        if node.type == "field_declaration":
            start, end = _lines(node)
            first = source[node.start_byte:node.end_byte].decode("utf-8", errors="replace").split("\n", 1)[0]
            field = {"name": _al_strip_quotes(_al_member_name(node, source) or "?"),
                     "sig": _one_line(first), "start": start, "end": end, "triggers": []}
            fields.append(field)
        for child in node.children:
            walk(child, field)

    for child in obj.children:
        walk(child, None)
    return decls, fields


def _parse_lines(lines) -> list[int]:
    """[728, 2738] from a list of ints/strings or a string such as 'L728, 2738'."""
    if lines is None:
        return []
    items = lines if isinstance(lines, (list, tuple)) else re.split(r"[\s,;]+", str(lines))
    out = []
    for it in items:
        m = re.fullmatch(r"L?(\d+)", str(it).strip())
        if m:
            out.append(int(m.group(1)))
    return out


def _locate_lines(header: str, source_path: Path, start: int, end: int,
                  decls: list[dict], fields: list[dict], wanted: list[int]) -> str:
    out = [f"{header}  [{source_path.name} L{start}-{end}]"]
    for ln in wanted:
        inner = min((d for d in decls if d["start"] <= ln <= d["end"]),
                    key=lambda d: d["end"] - d["start"], default=None)
        if inner is not None:
            out.append(f"L{ln} -> {inner['kind']} {inner['name']}  L{inner['start']}-{inner['end']}")
            continue
        fld = next((f for f in fields if f["start"] <= ln <= f["end"]), None)
        if fld is not None:
            out.append(f"L{ln} -> field {fld['name']}  L{fld['start']}-{fld['end']} (declaration)")
        elif start <= ln <= end:
            out.append(f"L{ln} -> object level (not inside a procedure, trigger or field)")
        else:
            out.append(f"L{ln} -> outside this object (L{start}-{end})")
    return "\n".join(out)


def get_outline(source_path: Path, source_location: str | None,
                expected_name: str | None = None, pattern: str | None = None,
                section: str = "summary", max_items: int = 300, lines=None) -> str:
    """Table of contents of the AL object at `source_location`: its procedures,
    triggers, fields and event publishers with 1-based line ranges, read from the
    current file (so it is never stale). `summary` gives counts and object
    triggers only; `pattern` filters by name across every section. `lines`
    (e.g. grep hits) maps each line number to the innermost procedure, trigger or
    field that contains it, one row per line, instead of listing a section."""
    spans = _resolve_spans(source_path, source_location, expected_name)
    obj, source = spans["object"], spans["source"]
    if obj is None:
        raise SourceLookupError("No AL object declaration found at that location.")
    section = (section or "summary").lower()
    if section not in _OUTLINE_SECTIONS:
        raise SourceLookupError(f"Unknown section '{section}'; use one of {', '.join(_OUTLINE_SECTIONS)}.")
    if pattern and section == "summary":
        section = "all"
    decls, fields = _outline_entries(obj, source)
    start, end = _lines(obj)
    header = _one_line(_header_text(obj, source).split("\n", 1)[0])
    wanted = _parse_lines(lines)
    if wanted:
        return _locate_lines(header, source_path, start, end, decls, fields, wanted)
    procs = [d for d in decls if d["kind"] == "procedure" and not d["event"]]
    events = [d for d in decls if d["event"]]
    obj_triggers = [d for d in decls if d["kind"] == "trigger" and not d["member_trigger"]]
    member_triggers = [d for d in decls if d["member_trigger"]]
    out = [f"{header}  [{source_path.name} L{start}-{end}, {end - start + 1} lines]",
           f"{len(procs)} procedures, {len(events)} event publishers, "
           f"{len(fields)} fields ({sum(1 for f in fields if f['triggers'])} with triggers), "
           f"{len(obj_triggers)} object triggers, {len(member_triggers)} member triggers"]

    def keep(name: str) -> bool:
        return not pattern or pattern.lower() in name.lower()

    rows: list[str] = []
    if section == "summary":
        rows += [f"L{d['start']}-{d['end']} {d['sig']}" for d in obj_triggers]
        out += rows
        out.append("Sections: procedures | triggers | fields | events | all; or pass pattern=<name part>.")
        return "\n".join(out)
    if section in ("triggers", "all"):
        rows += [f"L{d['start']}-{d['end']} trigger {d['name']}"
                 for d in obj_triggers + member_triggers if keep(d["name"])]
    if section in ("procedures", "all"):
        rows += [f"L{d['start']}-{d['end']} {d['sig']}" for d in procs if keep(d["name"])]
    if section in ("fields", "all"):
        rows += [f"L{f['start']}-{f['end']} {f['sig']}"
                 + (f"  [{', '.join(f['triggers'])}]" if f["triggers"] else "")
                 for f in fields if keep(f["name"])]
    if section in ("events", "all"):
        rows += [f"L{d['start']}-{d['end']} [{d['event']} event] {d['sig']}"
                 for d in events if keep(d["name"])]
    if not rows:
        rows = [f"(nothing in section '{section}'" + (f" matching '{pattern}')" if pattern else ")")]
    if len(rows) > max_items:
        rows = rows[:max_items] + [f"... +{len(rows) - max_items} more (narrow with pattern)"]
    return "\n".join(out + rows)
