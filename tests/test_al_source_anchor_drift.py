"""Source lookups must never return a different symbol's text when the file drifts.

The graph stores only a start line (`source_location = "L<n>"`) and
source_lookup re-parses the file on demand. When the source on disk is a newer
revision than the graph (lines inserted above the symbol), the stored line lands
inside another procedure and its body is returned with no error. Observed on the
public bc-code-atlas instance on 2026-09-30: `get_procedure_body` for
`Sales Line.InitQty` (graph: L6055) returned the whole of `UpdateVATAmounts`,
because InitQty had moved to L6081 in the served file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_al")

from graphify.source_lookup import SourceLookupError, get_procedure_body, get_signature

TABLE = (
    "table 50100 \"My Line\"\n"          # 1
    "{\n"                                # 2
    "    fields\n"                       # 3
    "    {\n"                            # 4
    "        field(15; Quantity; Decimal)\n"  # 5
    "        {\n"                        # 6
    "            trigger OnValidate()\n"  # 7
    "            begin\n"                # 8
    "                InitQty();\n"       # 9
    "            end;\n"                 # 10
    "        }\n"                        # 11
    "    }\n"                            # 12
    "\n"                                 # 13
    "    procedure UpdateVATAmounts()\n"  # 14
    "    begin\n"                        # 15
    "        Message('vat');\n"          # 16
    "    end;\n"                         # 17
    "\n"                                 # 18
    "    local procedure InitQty()\n"    # 19
    "    begin\n"                        # 20
    "        Message('init');\n"         # 21
    "    end;\n"                         # 22
    "}\n"                                # 23
)


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "MyLine.Table.al"
    p.write_text(text, encoding="utf-8")
    return p


def _drifted(text: str, lines_above: int, at_line: int) -> str:
    """Insert blank-ish procedures above `at_line` so later symbols move down."""
    rows = text.split("\n")
    filler = ["    // added upstream"] * lines_above
    return "\n".join(rows[: at_line - 1] + filler + rows[at_line - 1:])


def test_field_trigger_body_resolves_with_current_line(tmp_path: Path) -> None:
    """A field-level trigger is reachable when the stored line is current.

    Confirms the live `Sales Line.Quantity.OnValidate` failure is drift, not a
    grammar gap: the trigger itself resolves fine."""
    p = _write(tmp_path, TABLE)

    body = get_procedure_body(p, "L7")

    assert body.startswith("trigger OnValidate()")
    assert "InitQty();" in body


@pytest.mark.xfail(strict=True, reason="a line alone cannot detect drift; callers must pass expected_name")
def test_drift_without_anchor_returns_foreign_body(tmp_path: Path) -> None:
    """Documents the limit: a line-only lookup after drift returns another symbol."""
    p = _write(tmp_path, _drifted(TABLE, lines_above=5, at_line=14))

    body = get_procedure_body(p, "L19")  # InitQty's line before the drift

    assert "UpdateVATAmounts" not in body or "InitQty" in body, (
        "line-only lookup silently returned a different procedure"
    )


def test_drift_with_expected_name_finds_moved_symbol(tmp_path: Path) -> None:
    p = _write(tmp_path, _drifted(TABLE, lines_above=5, at_line=14))

    body = get_procedure_body(p, "L19", expected_name="InitQty")

    assert body.startswith("local procedure InitQty()")
    assert "Message('init');" in body


def test_drift_with_expected_name_signature(tmp_path: Path) -> None:
    p = _write(tmp_path, _drifted(TABLE, lines_above=5, at_line=14))

    assert get_signature(p, "L19", expected_name="InitQty") == "local procedure InitQty()"


def _graph(path: Path):
    import networkx as nx

    from graphify.extract import extract_al

    result = extract_al(path)
    G = nx.DiGraph()
    for n in result["nodes"]:
        G.add_node(n["id"], **n)
    for e in result["edges"]:
        G.add_edge(e["source"], e["target"], **e)
    return G


def _node(G, label: str, under: str | None = None) -> str:
    from graphify.serve import al_expected_name

    return next(nid for nid, d in G.nodes(data=True)
                if d.get("label") == label
                and (under is None or (al_expected_name(G, nid) or "").startswith(under)))


def test_graph_builds_member_qualified_expected_names(tmp_path: Path) -> None:
    from graphify.serve import al_expected_name

    G = _graph(_write(tmp_path, TABLE))

    assert al_expected_name(G, _node(G, ".OnValidate()")) == "Quantity.OnValidate"
    assert al_expected_name(G, _node(G, ".InitQty()")) == "InitQty"


def test_graph_anchor_survives_drift_end_to_end(tmp_path: Path) -> None:
    """Graph built on one revision, source read from a newer one."""
    from graphify.serve import al_expected_name

    p = _write(tmp_path, TABLE)
    G = _graph(p)
    trig, init = _node(G, ".OnValidate()"), _node(G, ".InitQty()")
    p.write_text(_drifted(TABLE, lines_above=26, at_line=3), encoding="utf-8")

    for nid, head in ((trig, "trigger OnValidate()"), (init, "local procedure InitQty()")):
        d = G.nodes[nid]
        body = get_procedure_body(p, d["source_location"], expected_name=al_expected_name(G, nid))
        assert body.startswith(head)


def test_missing_symbol_raises_stale_instead_of_guessing(tmp_path: Path) -> None:
    p = _write(tmp_path, TABLE.replace("InitQty()\n    begin", "InitQuantity()\n    begin"))

    with pytest.raises(SourceLookupError, match="stale"):
        get_procedure_body(p, "L19", expected_name="InitQty")
