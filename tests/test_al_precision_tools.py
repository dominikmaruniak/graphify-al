"""AL precision-retrieval tools: symbol paths, compact neighbors, outline, caps.

Fixtures are synthetic AL (invented 50xxx objects) run through the full
`extract()` pipeline, so event-publisher flags and cross-object call edges are
the ones production graphs carry. Tools are exercised end-to-end over the
in-process Streamable HTTP transport, as in test_serve_resolve_node.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_al", reason="tree-sitter-al not installed")
pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from graphify import serve as serve_mod  # noqa: E402
from graphify.al_precision import parse_symbol_path  # noqa: E402
from graphify.extract import extract  # noqa: E402

_TABLE = """table 50100 "Widget Line"
{
    fields
    {
        field(1; "No."; Code[20])
        {
            trigger OnValidate()
            begin
                InitQty();
            end;
        }
        field(15; Quantity; Decimal)
        {
            trigger OnValidate()
            begin
                OnBeforeValidateQuantity(Rec);
                InitQty();
            end;
        }
    }

    trigger OnInsert()
    var
        WidgetPost: Codeunit "Widget Post";
    begin
        WidgetPost.Post(Rec);
    end;

    procedure InitQty()
    begin
        Message('init');
    end;

    procedure GetHeader(var H: Record Customer)
    begin
    end;

    procedure GetHeader(var H: Record Customer; No: Code[20])
    begin
    end;

    [IntegrationEvent(false, false)]
    local procedure OnBeforeValidateQuantity(var WidgetLine: Record "Widget Line")
    begin
    end;
}
"""

_CODEUNIT = """codeunit 50101 "Widget Post"
{
    procedure Post(var WidgetLine: Record "Widget Line")
    begin
    end;
}
"""

_H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@pytest.fixture()
def graph(tmp_path: Path) -> str:
    files = []
    for name, src in (("WidgetLine.Table.al", _TABLE), ("WidgetPost.Codeunit.al", _CODEUNIT)):
        (tmp_path / name).write_text(src, encoding="utf-8")
        files.append(tmp_path / name)
    result = extract(files, cache_root=tmp_path)
    nodes = []
    for n in result["nodes"]:
        n = dict(n, community=0)
        if n.get("source_file"):
            n["source_file"] = Path(n["source_file"]).name
        nodes.append(n)
    out = tmp_path / "graphify-out"
    out.mkdir(exist_ok=True)
    gp = out / "graph.json"
    gp.write_text(json.dumps({"directed": True, "nodes": nodes, "edges": [
        dict(e, confidence=e.get("confidence", "EXTRACTED")) for e in result["edges"]]}), encoding="utf-8")
    return str(gp)


def _call(graph_path: str, name: str, arguments: dict) -> str:
    app = serve_mod._build_http_app(graph_path, json_response=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        init = client.post("/mcp", headers=_H, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}}})
        headers = {**_H, "mcp-session-id": init.headers["mcp-session-id"]}
        client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        resp = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}})
        return "\n".join(c["text"] for c in resp.json()["result"]["content"])


# --- symbol paths -------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ('Table 37 "Sales Line".Quantity.OnValidate', ("table", 37, "Sales Line", ["Quantity", "OnValidate"])),
    ('"Sales Line".InitQty()', (None, None, "Sales Line", ["InitQty"])),
    ('Codeunit "Sales-Post"', ("codeunit", None, "Sales-Post", [])),
    ('Customer."No."', (None, None, "Customer", ["No."])),
    ("InitQty", None),  # a bare word stays an ordinary label lookup
])
def test_parse_symbol_path(text, expected):
    assert parse_symbol_path(text) == expected


def test_body_by_symbol_path_for_field_trigger(graph):
    out = _call(graph, "bcatlas_get_procedure_body", {"label": '"Widget Line".Quantity.OnValidate'})
    assert out.startswith("trigger OnValidate()")
    assert "InitQty();" in out


def test_unknown_member_path_is_a_clear_miss(graph):
    out = _call(graph, "bcatlas_get_procedure_body", {"label": '"Widget Line".NoSuchProc'})
    assert out.startswith("No AL symbol matches the path")


# --- compact neighbors ----------------------------------------------------------

def test_compact_neighbors_splits_events_from_calls(graph, monkeypatch):
    out = _call(graph, "bcatlas_get_neighbors",
                {"label": 'Table "Widget Line".Quantity.OnValidate', "format": "compact"})
    lines = out.splitlines()
    assert lines[0].startswith('Table 50100 "Widget Line".Quantity.OnValidate')
    assert "raises (1): .OnBeforeValidateQuantity" in out
    assert "calls (1): .InitQty" in out


def test_compact_neighbors_prints_foreign_callers_as_full_paths(graph):
    out = _call(graph, "bcatlas_get_neighbors", {"label": 'Codeunit "Widget Post".Post', "format": "compact"})
    assert 'called by (1): Table 50100 "Widget Line".OnInsert' in out


def test_member_names_with_dots_are_quoted_and_round_trip(graph):
    """`No.` must print as `."No.".OnValidate` -- `.No..OnValidate` is unparseable."""
    out = _call(graph, "bcatlas_get_neighbors", {"label": '"Widget Line".InitQty', "format": "compact"})
    assert '."No.".OnValidate' in out
    body = _call(graph, "bcatlas_get_procedure_body", {"label": '"Widget Line"."No.".OnValidate'})
    assert body.startswith("trigger OnValidate()")


def test_full_format_stays_the_default_without_precision_mode(graph, monkeypatch):
    monkeypatch.delenv("GRAPHIFY_AL_PRECISION", raising=False)
    out = _call(graph, "bcatlas_get_neighbors", {"label": '"Widget Line".InitQty'})
    assert out.startswith("Neighbors of ")


def test_precision_mode_makes_compact_the_default(graph, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_AL_PRECISION", "1")
    out = _call(graph, "bcatlas_get_neighbors", {"label": '"Widget Line".InitQty'})
    assert out.startswith('Table 50100 "Widget Line".InitQty')


# --- body location header -------------------------------------------------------

def test_with_location_prefixes_path_file_and_range(graph):
    out = _call(graph, "bcatlas_get_procedure_body",
                {"label": '"Widget Line".GetHeader', "with_location": True})
    first = out.splitlines()[0]
    assert first.startswith('// Table 50100 "Widget Line".GetHeader | WidgetLine.Table.al L')
    assert first.count("-") >= 2  # two overload ranges


# --- outline ------------------------------------------------------------------------

def test_outline_summary_counts(graph):
    out = _call(graph, "bcatlas_get_outline", {"label": 'Table "Widget Line"'})
    assert out.splitlines()[0].startswith('table 50100 "Widget Line"')
    assert "3 procedures, 1 event publishers, 2 fields (2 with triggers), 1 object triggers, 2 member triggers" in out
    assert "trigger OnInsert()" in out


def test_outline_pattern_searches_every_section(graph):
    out = _call(graph, "bcatlas_get_outline", {"label": 'Table "Widget Line"', "pattern": "quantity"})
    assert "trigger Quantity.OnValidate" in out
    assert "field(15; Quantity; Decimal)  [OnValidate L" in out
    assert "[integration event] local procedure OnBeforeValidateQuantity" in out
    assert "InitQty" not in out


# --- object source cap ------------------------------------------------------------------

def test_object_source_over_max_lines_returns_outline(graph):
    out = _call(graph, "bcatlas_get_object_source", {"label": 'Table "Widget Line"', "max_lines": 10})
    assert out.startswith("// Object source is")
    assert 'table 50100 "Widget Line"' in out


def test_object_source_zero_max_lines_returns_everything(graph):
    out = _call(graph, "bcatlas_get_object_source", {"label": 'Table "Widget Line"', "max_lines": 0})
    assert out.startswith('table 50100 "Widget Line"') and out.rstrip().endswith("}")
