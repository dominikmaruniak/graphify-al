"""Calls through Record-typed variables resolve to table procedures.

`SalesLine.InitQty()` on a `Record "Sales Line"` variable is how most of Business
Central calls table logic, but only Codeunit-typed and Interface-typed receivers
produced `calls` edges, so "who calls Sales Line.InitQty" missed every caller in
another object. Record calls now resolve to the procedure on the table or on one
of its tableextensions; platform methods (Get, SetRange, Validate, ...) and
unknown members of an in-corpus table produce no edge; a member of a table
outside the corpus becomes a `Table.Method` stub, like codeunit calls do.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_al", reason="tree-sitter-al not installed")

from graphify.extract import extract  # noqa: E402

_TABLE = """table 50100 "Widget Line"
{
    fields { field(1; "No."; Code[20]) { } }

    procedure InitQty()
    begin
    end;
}
"""

_TABLE_EXT = """tableextension 50102 "Widget Line Ext" extends "Widget Line"
{
    procedure ExtHelper()
    begin
    end;
}
"""

_CODEUNIT = """codeunit 50101 "Widget Post"
{
    procedure Post()
    var
        WidgetLine: Record "Widget Line";
        Cust: Record Customer;
    begin
        WidgetLine.SetRange("No.", 'A');
        if WidgetLine.FindSet() then
            WidgetLine.InitQty();
        WidgetLine.ExtHelper();
        WidgetLine.Validate("No.", 'B');
        WidgetLine.NotDeclaredAnywhere();
        Cust.Get('C');
        Cust.MyCustomerHelper();
    end;
}
"""


@pytest.fixture()
def graph(tmp_path: Path) -> dict:
    files = []
    for name, src in (("WidgetLine.Table.al", _TABLE), ("WidgetLineExt.TableExt.al", _TABLE_EXT),
                      ("WidgetPost.Codeunit.al", _CODEUNIT)):
        (tmp_path / name).write_text(src, encoding="utf-8")
        files.append(tmp_path / name)
    return extract(files, cache_root=tmp_path)


def _calls_from_post(g: dict) -> dict[str, str]:
    labels = {n["id"]: n["label"] for n in g["nodes"]}
    post = next(n["id"] for n in g["nodes"] if n["label"] == ".Post()")
    return {labels.get(e["target"], e["target"]): e["target"]
            for e in g["edges"] if e["relation"] == "calls" and e["source"] == post}


def test_record_call_resolves_to_table_procedure(graph):
    calls = _calls_from_post(graph)
    assert ".InitQty()" in calls
    assert "_table_widget_line_initqty" in calls[".InitQty()"]


def test_record_call_resolves_to_tableextension_procedure(graph):
    calls = _calls_from_post(graph)
    assert ".ExtHelper()" in calls
    assert "widget_line_ext" in calls[".ExtHelper()"]


def test_platform_methods_and_unknown_members_add_no_edges(graph):
    calls = _calls_from_post(graph)
    assert not any(t.lower().rstrip("()").lstrip(".") in
                   ("setrange", "findset", "validate", "get", "notdeclaredanywhere")
                   for t in calls)
    # no object-level fallback edge onto the table itself
    assert not any("Widget Line" in t and not t.startswith(".") for t in calls)


def test_member_of_table_outside_corpus_becomes_stub(graph):
    calls = _calls_from_post(graph)
    assert "Customer.MyCustomerHelper" in calls
    assert "Customer.Get" not in calls
