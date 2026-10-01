"""Neighbor views that answer multi-hop AL questions in one call.

- `raises` lists each raised event with its subscribers, so "which events of this
  trigger have subscribers" no longer takes one get_neighbors call per event.
- An object lists the implicit platform events (`OnAfterDeleteEvent`, ...) that
  have subscribers; the event resolves as a member and explains it has no body.
- Callers of an overloaded procedure are split per overload by the argument
  count at the call site, and a same-named method on another object is ignored.

Synthetic AL through the full extract() pipeline, tools called over the
in-process Streamable HTTP transport, as in test_al_precision_tools.py.
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
from graphify.al_overloads import call_arg_counts, param_count  # noqa: E402
from graphify.extract import extract  # noqa: E402

_TABLE = """table 50120 "Gizmo"
{
    fields
    {
        field(1; "No."; Code[20])
        {
            trigger OnValidate()
            begin
                OnBeforeValidateNo(Rec);
                Calc(1);
                OnAfterValidateNo(Rec);
            end;
        }
    }

    procedure Calc(A: Integer)
    begin
    end;

    procedure Calc(A: Integer; B: Integer)
    begin
    end;

    [IntegrationEvent(false, false)]
    local procedure OnBeforeValidateNo(var Gizmo: Record Gizmo)
    begin
    end;

    [IntegrationEvent(false, false)]
    local procedure OnAfterValidateNo(var Gizmo: Record Gizmo)
    begin
    end;
}
"""

_MGT = """codeunit 50121 "Gizmo Mgt"
{
    procedure Run()
    var
        G: Record Gizmo;
        Other: Codeunit "Other Calc";
    begin
        G.Calc(1, StrSubstNo('%1,%2', 3, 4));
        Other.Calc(1);
    end;
}
"""

_OTHER = """codeunit 50122 "Other Calc"
{
    procedure Calc(A: Integer)
    begin
    end;
}
"""

_SUBS = """codeunit 50123 "Gizmo Subs"
{
    [EventSubscriber(ObjectType::Table, Database::Gizmo, 'OnBeforeValidateNo', '', false, false)]
    local procedure HandleBeforeValidateNo(var Gizmo: Record Gizmo)
    begin
    end;

    [EventSubscriber(ObjectType::Table, Database::Gizmo, 'OnAfterDeleteEvent', '', false, false)]
    local procedure CleanUp(var Rec: Record Gizmo; RunTrigger: Boolean)
    begin
    end;
}
"""

_H = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture()
def graph(tmp_path: Path) -> str:
    files = []
    for name, src in (("Gizmo.Table.al", _TABLE), ("GizmoMgt.Codeunit.al", _MGT),
                      ("OtherCalc.Codeunit.al", _OTHER), ("GizmoSubs.Codeunit.al", _SUBS)):
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


def _neighbors(graph: str, label: str) -> str:
    return _call(graph, "bcatlas_get_neighbors", {"label": label, "format": "compact"})


def test_raises_lists_subscribers_per_event(graph):
    out = _neighbors(graph, '"Gizmo"."No.".OnValidate')
    assert "raises (2; 1 with subscribers):" in out
    assert '.OnBeforeValidateNo <- Codeunit 50123 "Gizmo Subs".HandleBeforeValidateNo' in out
    assert "no subscribers in the graph: .OnAfterValidateNo" in out


def test_object_lists_implicit_events_with_subscribers(graph):
    out = _neighbors(graph, 'Table "Gizmo"')
    assert "implicit events with subscribers: OnAfterDeleteEvent (1)" in out


def test_implicit_event_resolves_and_lists_its_subscribers(graph):
    out = _neighbors(graph, '"Gizmo".OnAfterDeleteEvent')
    assert "implicit platform event" in out
    assert 'subscribed by (1): Codeunit 50123 "Gizmo Subs".CleanUp' in out
    card = _call(graph, "bcatlas_resolve_node",
                 {"object_type": "table", "object_name": "Gizmo", "member": "OnAfterDeleteEvent"})
    assert "_table_gizmo_onafterdeleteevent" in card


def test_implicit_event_has_no_body_and_says_so(graph):
    out = _call(graph, "bcatlas_get_procedure_body", {"label": '"Gizmo".OnAfterDeleteEvent'})
    assert "implicit platform event" in out
    assert "stale" not in out.lower()


def test_callers_split_per_overload(graph):
    out = _neighbors(graph, '"Gizmo".Calc')
    assert "split by overload" in out
    lines = out.splitlines()
    one = next(line for line in lines if "(1 params)" in line)
    two = next(line for line in lines if "(2 params)" in line)
    assert '."No.".OnValidate' in one and "Gizmo Mgt" not in one
    assert 'Codeunit 50121 "Gizmo Mgt".Run' in two
    assert "overload not determined" not in out


def test_param_and_argument_counts():
    assert param_count("procedure Calc(A: Integer; var B: Record \"Sales Line\")") == 2
    assert param_count("local procedure OnRun()") == 0
    body = "X.Calc(1, StrSubstNo('%1,%2', 3, 4));\n// Calc(9, 9, 9);\nCalc();\nY.Calc(Arr[1, 2])"
    counts = call_arg_counts(body, "Calc", lambda v: {"X": True, "Y": None}.get(v, False), True)
    assert counts == [2, 0, None]
