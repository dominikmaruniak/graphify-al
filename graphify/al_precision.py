"""AL precision-retrieval helpers over the structural graph.

Symbol paths address AL declarations the way a developer names them --
`Table 37 "Sales Line".Quantity.OnValidate`, `Codeunit "Sales-Post".PostSalesLine`
-- and resolve deterministically over the graph's contains/method/trigger edges
instead of fuzzy label matching. The compact neighbor view prints each neighbor
once, as a symbol path that the source tools accept back, and keeps same-object
neighbors short (`.InitQty`).

`GRAPHIFY_AL_PRECISION=1` makes the compact/capped behaviours the default in
serve.py; without it only explicit tool arguments opt in.
"""
from __future__ import annotations

import os
import re

import networkx as nx

from graphify.build import edge_data

_OWNER_RELATIONS = ("contains", "method", "trigger")

_AL_TYPES = (
    "tableextension", "table", "pageextension", "page", "codeunit", "reportextension",
    "report", "query", "xmlport", "enumextension", "enum", "interface", "controladdin",
    "permissionsetextension", "permissionset", "profileextension", "profile", "entitlement",
)
_TYPE_RE = re.compile(r"^(" + "|".join(_AL_TYPES) + r")\s+", re.IGNORECASE)
_LABEL_RE = re.compile(r'^(?P<type>\w+) (?P<id>\d+) (?:"(?P<q>.*)"|(?P<b>\S+))$')
_MEMBER_RE = re.compile(r'\.(?:"([^"]+)"|([^."]+))')


def precision_mode() -> bool:
    return os.environ.get("GRAPHIFY_AL_PRECISION", "").strip().lower() in ("1", "true", "yes", "on")


def _al_bare_member(label: str) -> str:
    """`.InitQty()` -> `InitQty`, `."Quantity (Base)"` -> `Quantity (Base)`."""
    s = label[1:] if label.startswith(".") else label
    if s.endswith("()"):
        s = s[:-2]
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s


def _label(G: nx.Graph, nid: str) -> str:
    return str(G.nodes[nid].get("label") or "")


def _owner(G: nx.Graph, nid: str) -> str | None:
    return next((p for p in G.predecessors(nid)
                 if edge_data(G, p, nid).get("relation") in _OWNER_RELATIONS), None)


def al_expected_name(G: nx.Graph, nid: str) -> str | None:
    """`Member.Name` path of an AL member node inside its object, built from the
    graph (`Quantity.OnValidate` for a field trigger, `InitQty` for a procedure,
    `Quantity` for a field). Object and file nodes return None.

    source_lookup verifies the declaration it finds against this path, so a file
    that drifted since the graph was built never returns another symbol's text.
    Names may themselves contain dots (`No.`); use al_member_parts to display."""
    parts = al_member_parts(G, nid)
    return ".".join(parts) if parts else None


def al_member_parts(G: nx.Graph, nid: str) -> list[str] | None:
    """Member path segments, outermost first: ["No.", "OnValidate"]."""
    label = _label(G, nid)
    if not label.startswith("."):
        return None
    parts = [_al_bare_member(label)]
    current = nid
    for _ in range(8):
        owner = next(
            (p for p in G.predecessors(current)
             if edge_data(G, p, current).get("relation") in ("contains", "trigger")
             and _label(G, p).startswith(".")),
            None,
        )
        if owner is None:
            break
        parts.append(_al_bare_member(_label(G, owner)))
        current = owner
    return list(reversed(parts))


def al_object_of(G: nx.Graph, nid: str) -> str | None:
    """The AL object node that owns `nid` (itself for an object node)."""
    current = nid
    for _ in range(10):
        if not _label(G, current).startswith("."):
            return current if _LABEL_RE.match(_label(G, current)) else None
        owner = _owner(G, current)
        if owner is None:
            return None
        current = owner
    return None


def _object_ref(label: str) -> str:
    m = _LABEL_RE.match(label)
    if not m:
        return label
    name = m.group("q") if m.group("q") is not None else m.group("b")
    return f'{m.group("type")} {m.group("id")} "{name}"'


def al_symbol_path(G: nx.Graph, nid: str) -> str:
    """`Table 37 "Sales Line".Quantity.OnValidate` -- the path resolve_symbol_path
    accepts back. Falls back to the node label for non-AL nodes."""
    obj = al_object_of(G, nid)
    if obj is None:
        return _label(G, nid)
    ref = _object_ref(_label(G, obj))
    if obj == nid:
        return ref
    parts = al_member_parts(G, nid)
    return f"{ref}.{_quote_parts(parts)}" if parts else ref


def _quote_parts(parts: list[str]) -> str:
    """Join member segments, quoting any that is not a plain identifier, so a
    field like `No.` renders as `"No."` and the path parses back unambiguously."""
    return ".".join(f'"{p}"' if re.search(r"\W", p) else p for p in parts)


def parse_symbol_path(text: str) -> tuple[str | None, int | None, str, list[str]] | None:
    """(type, id, object name, member segments) or None when `text` is not a path.

    Accepts `Table 37 "Sales Line".Quantity.OnValidate`, `"Sales Line".InitQty()`,
    `Codeunit "Sales-Post"`, `Customer.Name`. A bare word with no quotes, no type
    and no member is not treated as a path (it stays an ordinary label lookup)."""
    s = text.strip()
    typ = None
    m = _TYPE_RE.match(s)
    if m:
        typ, s = m.group(1).lower(), s[m.end():]
    oid = None
    m = re.match(r"^(\d+)\s+", s)
    if m:
        oid, s = int(m.group(1)), s[m.end():]
    if s.startswith('"'):
        end = s.find('"', 1)
        if end < 0:
            return None
        name, rest = s[1:end], s[end + 1:]
    else:
        dot = s.find(".")
        name, rest = (s, "") if dot < 0 else (s[:dot], s[dot:])
        name = name.strip()
    if not name:
        return None
    members = [_al_bare_member("." + (q or b).strip()) for q, b in _MEMBER_RE.findall(rest)]
    if rest.strip() and not members:
        return None
    if typ is None and oid is None and not members and not text.strip().startswith('"'):
        return None
    return typ, oid, name, members


def _object_index(G: nx.Graph) -> dict[str, list[tuple[str, str, int]]]:
    cache = G.graph.get("_al_object_index")
    if cache is not None and cache[0] == G.number_of_nodes():
        return cache[1]
    index: dict[str, list[tuple[str, str, int]]] = {}
    for nid, d in G.nodes(data=True):
        m = _LABEL_RE.match(str(d.get("label") or ""))
        if not m:
            continue
        name = m.group("q") if m.group("q") is not None else m.group("b")
        index.setdefault(name.lower(), []).append((nid, m.group("type").lower(), int(m.group("id"))))
    G.graph["_al_object_index"] = (G.number_of_nodes(), index)
    return index


def resolve_symbol_path(G: nx.Graph, text: str) -> list[str] | None:
    """Node ids for a symbol path, or None when `text` is not a symbol path.

    An empty list means the path parsed but matched nothing; several ids mean it
    is ambiguous (e.g. a table and a page with the same name and no type given)."""
    parsed = parse_symbol_path(text)
    if parsed is None:
        return None
    typ, oid, name, members = parsed
    objs = [nid for nid, t, i in _object_index(G).get(name.lower(), [])
            if (typ is None or t == typ) and (oid is None or i == oid)]
    results: list[str] = []
    for obj in objs:
        frontier = [obj]
        for seg in members:
            frontier = [s for cur in frontier for s in G.successors(cur)
                        if edge_data(G, cur, s).get("relation") in _OWNER_RELATIONS
                        and _al_bare_member(_label(G, s)).lower() == seg.lower()]
        results += frontier
    return results


_IN_NAMES = {"calls": "called by", "subscribes": "subscribed by", "extends": "extended by",
             "implements": "implemented by", "binds": "bound by", "relates_to": "related from",
             "references": "referenced by"}


def compact_neighbors(G: nx.Graph, nid: str, relation_filter: str = "",
                      max_per_relation: int = 60) -> str:
    """Neighbors grouped by relation, one line each, every neighbor printed once
    as a symbol path (`.Name` when it lives in the same object as the seed).
    Calls into event publishers are listed separately as `raises`."""
    seed_obj = al_object_of(G, nid)
    d = G.nodes[nid]
    loc = f"{d.get('source_file', '')} {d.get('source_location', '')}".strip()
    lines = [f"{al_symbol_path(G, nid)}  [{loc}]"]

    def ref(other: str) -> str:
        if seed_obj is not None and al_object_of(G, other) == seed_obj and other != seed_obj:
            parts = al_member_parts(G, other)
            if parts:
                return "." + _quote_parts(parts)
        return al_symbol_path(G, other)

    groups: dict[str, list[str]] = {}
    member_counts: dict[str, int] = {}
    for nb in G.successors(nid):
        rel = str(edge_data(G, nid, nb).get("relation", ""))
        if rel in _OWNER_RELATIONS and not _label(G, nid).startswith("."):
            kind = "fields/members" if rel == "contains" else ("triggers" if rel == "trigger" else "procedures")
            member_counts[kind] = member_counts.get(kind, 0) + 1
            continue
        if rel == "calls" and G.nodes[nb].get("event"):
            rel = "raises"
        groups.setdefault(rel, []).append(ref(nb))
    for nb in G.predecessors(nid):
        rel = str(edge_data(G, nb, nid).get("relation", ""))
        if rel in _OWNER_RELATIONS:
            continue
        groups.setdefault(_IN_NAMES.get(rel, f"{rel} (in)"), []).append(ref(nb))
    if member_counts:
        lines.append("members: " + ", ".join(f"{v} {k}" for k, v in member_counts.items())
                     + "  (list them with bcatlas_get_outline)")
    rf = relation_filter.lower()
    for rel, refs in groups.items():
        if rf and rf not in rel.lower():
            continue
        uniq = list(dict.fromkeys(refs))
        shown = ", ".join(uniq[:max_per_relation])
        more = f", ... +{len(uniq) - max_per_relation} more (narrow with relation_filter)" \
            if len(uniq) > max_per_relation else ""
        lines.append(f"{rel} ({len(uniq)}): {shown}{more}")
    if len(lines) == 1:
        lines.append("(no neighbors)")
    return "\n".join(lines)
