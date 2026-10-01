"""Implicit platform events, typed subscription targets, and case-insensitive calls.

A subscription to `Database::Customer, 'OnAfterDeleteEvent'` used to land on the
whole table node with the event name dropped, so "who reacts when a customer is
deleted" had no node to ask. Such events now get a member node of their own
(`."OnAfterDeleteEvent"()`, marked `event: implicit`) and every `subscribes` edge
keeps the attribute's event and element names. A same-named codeunit no longer
captures a table subscription, and an AL call whose casing differs from the
declaration (AL is case-insensitive) still produces its `calls` edge.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_al", reason="tree-sitter-al not installed")

from graphify.extract import extract  # noqa: E402

_TABLE = """table 50110 "Gadget"
{
    fields
    {
        field(1; "No."; Code[20])
        {
            trigger OnValidate()
            begin
                OnBeforeValidateNO(Rec);
                RecalcTotals();
            end;
        }
    }

    procedure RECALCTOTALS()
    begin
    end;

    [IntegrationEvent(false, false)]
    local procedure OnBeforeValidateNo(var Gadget: Record Gadget)
    begin
    end;
}
"""

_SAME_NAME_CODEUNIT = """codeunit 50111 "Gadget"
{
    procedure Nothing()
    begin
    end;
}
"""

_SUBSCRIBERS = """codeunit 50112 "Gadget Subscribers"
{
    [EventSubscriber(ObjectType::Table, Database::Gadget, 'OnAfterDeleteEvent', '', false, false)]
    local procedure CleanUpAfterDelete(var Rec: Record Gadget; RunTrigger: Boolean)
    begin
    end;

    [EventSubscriber(ObjectType::Table, Database::Gadget, 'OnAfterDeleteEvent', '', false, false)]
    local procedure AuditAfterDelete(var Rec: Record Gadget; RunTrigger: Boolean)
    begin
    end;

    [EventSubscriber(ObjectType::Table, Database::Gadget, 'OnBeforeInsertEvent', '', false, false)]
    local procedure StampBeforeInsert(var Rec: Record Gadget; RunTrigger: Boolean)
    begin
    end;

    [EventSubscriber(ObjectType::Table, Database::Gadget, 'OnAfterValidateEvent', 'No.', false, false)]
    local procedure AfterNoValidated(var Rec: Record Gadget; var xRec: Record Gadget)
    begin
    end;

    [EventSubscriber(ObjectType::Table, Database::Gadget, 'OnBeforeValidateNo', '', false, false)]
    local procedure BeforeNoValidated(var Gadget: Record Gadget)
    begin
    end;
}
"""


@pytest.fixture()
def graph(tmp_path: Path) -> dict:
    files = []
    for name, src in (("Gadget.Table.al", _TABLE), ("Gadget.Codeunit.al", _SAME_NAME_CODEUNIT),
                      ("GadgetSubscribers.Codeunit.al", _SUBSCRIBERS)):
        (tmp_path / name).write_text(src, encoding="utf-8")
        files.append(tmp_path / name)
    return extract(files, cache_root=tmp_path)


def _node(g: dict, label: str, id_part: str = "") -> dict:
    hits = [n for n in g["nodes"] if n["label"] == label and id_part in n["id"]]
    assert len(hits) == 1, (label, [n["id"] for n in hits])
    return hits[0]


def _subscribers_of(g: dict, target_id: str) -> dict[str, dict]:
    labels = {n["id"]: n["label"] for n in g["nodes"]}
    return {labels[e["source"]]: e for e in g["edges"]
            if e["relation"] == "subscribes" and e["target"] == target_id}


def test_implicit_event_gets_its_own_member_node(graph):
    table = _node(graph, 'Table 50110 "Gadget"') if any(
        n["label"] == 'Table 50110 "Gadget"' for n in graph["nodes"]) else None
    ev = _node(graph, ".OnAfterDeleteEvent()")
    assert ev["event"] == "implicit"
    assert "_table_gadget" in ev["id"]
    owners = [e for e in graph["edges"] if e["target"] == ev["id"] and e["relation"] == "method"]
    assert len(owners) == 1
    if table is not None:
        assert owners[0]["source"] == table["id"]


def test_implicit_event_collects_its_subscribers_only(graph):
    ev = _node(graph, ".OnAfterDeleteEvent()")
    subs = _subscribers_of(graph, ev["id"])
    assert set(subs) == {".CleanUpAfterDelete()", ".AuditAfterDelete()"}
    assert all(e["event"] == "OnAfterDeleteEvent" for e in subs.values())
    ins = _node(graph, ".OnBeforeInsertEvent()")
    assert set(_subscribers_of(graph, ins["id"])) == {".StampBeforeInsert()"}


def test_validate_event_subscription_stays_on_field_with_names_kept(graph):
    field_edges = [e for e in graph["edges"] if e["relation"] == "subscribes"
                   and e.get("event") == "OnAfterValidateEvent"]
    assert len(field_edges) == 1
    assert field_edges[0]["element"] == "No."
    assert field_edges[0]["target"].endswith("_table_gadget_no")


def test_declared_event_subscription_targets_the_table_not_same_named_codeunit(graph):
    ev = _node(graph, ".OnBeforeValidateNo()", "_table_gadget")
    assert ".BeforeNoValidated()" in _subscribers_of(graph, ev["id"])
    assert not any(e["relation"] == "subscribes" and "_codeunit_gadget" in e["target"]
                   and "subscribers" not in e["target"] for e in graph["edges"])


def test_calls_resolve_case_insensitively(graph):
    trig = next(n["id"] for n in graph["nodes"] if n["id"].endswith("_table_gadget_no_onvalidate"))
    labels = {n["id"]: n["label"] for n in graph["nodes"]}
    targets = {labels.get(e["target"]) for e in graph["edges"]
               if e["relation"] == "calls" and e["source"] == trig}
    assert ".OnBeforeValidateNo()" in targets
    assert ".RECALCTOTALS()" in targets
