"""Which overload of an AL procedure each caller uses.

AL overloads share one graph node, so "who calls GetSalesLines(4 params)" came back
mixed with every other overload's callers. AL has no optional parameters, so the
argument count at a call site picks the overload unless two overloads take the
same number of parameters. This re-reads the callers' source (never the index),
counts the arguments at each call of the procedure, checks that a qualified call
goes through a variable of the procedure's object type, and maps the count to the
overload declarations.
"""
from __future__ import annotations

import re
from pathlib import Path

import networkx as nx

from graphify import source_lookup
from graphify.al_precision import al_expected_name, al_object_of, _LABEL_RE, _label
from graphify.build import edge_data

MAX_CALLERS = 150
_VAR_TYPES = r"(?:Codeunit|Record|Page|Report|Query|XmlPort|Interface)"


def _split_count(text: str, open_at: int, sep: str) -> tuple[int, int] | None:
    """(number of top-level `sep`-separated items, index after the closing paren)
    for the parenthesised list starting at text[open_at] == '('."""
    depth, items, has, i = 0, 0, False, open_at
    while i < len(text):
        c = text[i]
        if c == "'":
            j = text.find("'", i + 1)
            while j != -1 and text[j + 1:j + 2] == "'":  # '' escapes a quote
                j = text.find("'", j + 2)
            if j == -1:
                return None
            has, i = True, j + 1
            continue
        if c == '"':
            j = text.find('"', i + 1)
            if j == -1:
                return None
            has, i = True, j + 1
            continue
        if c in "([":
            depth += 1
            if depth > 1:
                has = True
        elif c in ")]":
            depth -= 1
            if depth == 0:
                return (items + 1 if has else 0), i + 1
        elif c == sep and depth == 1:
            items += 1
        elif not c.isspace() and depth >= 1:
            has = True
        i += 1
    return None


def param_count(header: str) -> int | None:
    """Parameters in a `procedure Name(a: T; var b: T2) ...` header (';'-separated)."""
    m = re.search(r"\bprocedure\s+(?:\"[^\"]+\"|\w+)\s*\(", header, re.IGNORECASE)
    if not m:
        return None
    got = _split_count(header, m.end() - 1, ";")
    return got[0] if got else None


def call_arg_counts(body: str, name: str, receiver_ok, allow_unqualified: bool) -> list[int | None]:
    """Argument count of every call of `name` in `body`. `receiver_ok(var)` decides
    whether a qualified call `var.name(` reaches the target object; it returns
    True, False, or None (type unknown -> the count is reported as None). An
    unqualified call counts only inside the target's own object."""
    out: list[int | None] = []
    rx = re.compile(r"(?:(\w+)\s*\.\s*)?(?<![\w\"])" + re.escape(name) + r"\s*\(", re.IGNORECASE)
    for m in rx.finditer(body):
        line_start = body.rfind("\n", 0, m.start()) + 1
        if body[line_start:m.start()].lstrip().startswith("//"):
            continue
        before = body[line_start:m.start()]
        if re.search(r"\bprocedure\s*$", before, re.IGNORECASE):
            continue  # the declaration itself
        recv = m.group(1)
        verdict = allow_unqualified if recv is None else receiver_ok(recv)
        if verdict is False:
            continue
        got = _split_count(body, m.end() - 1, ",")
        out.append(got[0] if (got and verdict) else None)
    return out


def _object_name(G: nx.Graph, obj: str) -> str:
    m = _LABEL_RE.match(_label(G, obj))
    if not m:
        return _label(G, obj).strip('"')
    return m.group("q") if m.group("q") is not None else m.group("b")


def _spans(source_root: Path, G: nx.Graph, nid: str, cache: dict):
    d = G.nodes[nid]
    sf = d.get("source_file") or ""
    if not sf.lower().endswith(".al"):
        return None
    key = (sf, d.get("source_location"))
    if key not in cache:
        try:
            cache[key] = source_lookup._resolve_spans(
                source_root / sf, d.get("source_location"), al_expected_name(G, nid))
        except source_lookup.SourceLookupError:
            cache[key] = None
    return cache[key]


def callers_by_overload(G: nx.Graph, nid: str, source_root: Path):
    """None when `nid` is not an overloaded AL procedure (or has too many callers).
    Otherwise (overloads, per_caller): overloads is a list of (line, param count,
    header) in source order; per_caller maps caller node id -> sorted list of
    parameter counts it calls with, None standing for "could not tell"."""
    cache: dict = {}
    spans = _spans(source_root, G, nid, cache)
    if not spans or len(spans.get("group") or []) < 2:
        return None
    src = spans["source"]
    overloads = []
    for fn in spans["group"]:
        header = source_lookup._header_text(fn, src)
        overloads.append((fn.start_point[0] + 1, param_count(header), header))
    callers = [p for p in G.predecessors(nid) if edge_data(G, p, nid).get("relation") == "calls"]
    if len(callers) > MAX_CALLERS:
        return None
    name = (al_expected_name(G, nid) or "").split(".")[-1]
    target_obj = al_object_of(G, nid)
    target_name = _object_name(G, target_obj) if target_obj else ""
    target_key = target_name.lower()
    per_caller: dict[str, list[int | None]] = {}
    for caller in callers:
        cs = _spans(source_root, G, caller, cache)
        if not cs:
            per_caller[caller] = [None]
            continue
        csrc = cs["source"].decode("utf-8", errors="replace")
        fns = cs.get("group") or ([cs["function"]] if cs.get("function") is not None else [])
        bodies = [source_lookup._full_text(fn, cs["source"]) for fn in fns] or [csrc]
        same_object = al_object_of(G, caller) == target_obj

        def receiver_ok(var: str, _bodies=bodies, _csrc=csrc, _same=same_object):
            if var.lower() in ("rec", "xrec") and _same:
                return True
            decl = re.compile(r"\b" + re.escape(var) + r"\s*:\s*(?:temporary\s+)?" + _VAR_TYPES
                              + r'\s+(?:\w+\.)*(?:"([^"]+)"|(\w+))', re.IGNORECASE)
            for text in (*_bodies, _csrc):
                m = decl.search(text)
                if m:
                    return (m.group(1) or m.group(2)).lower() == target_key
            return None

        counts: list[int | None] = []
        for body in bodies:
            counts += call_arg_counts(body, name, receiver_ok, same_object)
        per_caller[caller] = sorted(set(counts), key=lambda c: (c is None, c)) or [None]
    return overloads, per_caller
