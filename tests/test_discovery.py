"""Proposals the column names do not produce.

Name matching is silent about four shapes: an opaquely named column, a
table's own key standing in for a reference, a composite key, and a
self-reference. Two mechanisms reach them. A composite key is reached by
name, from a child carrying every column of it. The rest are reached by
statistics `profile` already collected, which rule a pair out without
either column leaving the warehouse.

What is worth testing here is mostly what does *not* get proposed. A
discovery costs warehouse queries to check and a person's attention to
judge, so the pruning and the cap are the substance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stele.infer import (
    COMPOSITE_SCORE,
    DISCOVERY_SCORE,
    FKProposal,
    has_statistics,
    infer,
    propose_composite_keys,
    propose_from_statistics,
    validate_foreign_key,
)
from stele.overlay import write_overlay_stub
from stele.spec import DEFAULT_MIN_SCORE, ColumnSpec, ModelSpec, TableSpec


def _col(name: str, type_: str = "bigint", **kw: Any) -> ColumnSpec:
    return ColumnSpec(name=name, source_type=type_, **kw)


def _keyed(name: str, *columns: ColumnSpec, key: list[str]) -> TableSpec:
    return TableSpec(
        name=name,
        schema="dbo",
        columns=list(columns),
        primary_key=key,
        primary_key_origin="manual",
    )


def _spec(*tables: TableSpec) -> ModelSpec:
    return ModelSpec(catalog="cat", schemas=["dbo"], tables=list(tables))


def _ranged(
    name: str, low: int, high: int, distinct: int | None = None
) -> ColumnSpec:
    return _col(
        name,
        observed_min_value=low,
        observed_max_value=high,
        observed_distinct=distinct,
    )


def _stub(tmp_path: Path, *fks: FKProposal) -> str:
    path = tmp_path / "overlay.yaml"
    write_overlay_stub(ModelSpec(), [], list(fks), path)
    return path.read_text(encoding="utf-8")


def _triples(found: list[FKProposal]) -> list[tuple[str, str, str]]:
    return [(f.table, f.columns[0], f.referred_table) for f in found]


def _catalog() -> ModelSpec:
    """Two real references nothing names, among plausible coincidences.

    `Widget.Custodian` points at `Owner`, and `Widget.Flag` at `Status`.
    Neither column name says so. Every other pair that survives pruning is
    an integer sitting inside a wider integer range, which is the noise
    the ranking has to see past.
    """
    owner = _keyed("Owner", _ranged("OwnerId", 1, 512, 512), key=["OwnerId"])
    status = _keyed("Status", _ranged("StatusId", 1, 5, 5), key=["StatusId"])
    widget = _keyed(
        "Widget",
        _ranged("WidgetId", 1, 9000, 9000),
        _ranged("Custodian", 3, 498, 430),
        _ranged("Flag", 1, 3, 3),
        key=["WidgetId"],
    )
    return _spec(owner, status, widget)


# --- composite keys --------------------------------------------------------


def _composite_spec() -> ModelSpec:
    line = _keyed(
        "OrderLine",
        _col("OrderId"),
        _col("LineNo"),
        key=["OrderId", "LineNo"],
    )
    shipment = _keyed(
        "Shipment",
        _col("ShipmentId"),
        _col("OrderId"),
        _col("LineNo"),
        key=["ShipmentId"],
    )
    return _spec(line, shipment)


def test_a_child_carrying_every_key_column_is_proposed() -> None:
    (proposal,) = propose_composite_keys(_composite_spec())

    assert proposal.table == "dbo.Shipment"
    assert proposal.columns == ["OrderId", "LineNo"]
    assert proposal.referred_table == "dbo.OrderLine"
    assert proposal.score == COMPOSITE_SCORE
    assert proposal.basis == "composite"


def test_a_composite_proposal_is_written_live(tmp_path: Path) -> None:
    """All of a key matching by name is evidence enough to apply."""
    (proposal,) = propose_composite_keys(_composite_spec())

    text = _stub(tmp_path, proposal)

    assert "REJECTED" not in text
    assert '- columns: ["OrderId", "LineNo"]' in text


def test_a_child_missing_one_key_column_is_not_proposed() -> None:
    """Part of a key says nothing about how many columns the key spans."""
    spec = _composite_spec()
    shipment = spec.table("dbo.Shipment")
    assert shipment is not None
    shipment.columns = [c for c in shipment.columns if c.name != "LineNo"]

    assert propose_composite_keys(spec) == []


def test_a_type_that_disagrees_halves_a_composite_proposal() -> None:
    spec = _composite_spec()
    shipment = spec.table("dbo.Shipment")
    assert shipment is not None
    line_no = shipment.column("LineNo")
    assert line_no is not None
    line_no.source_type = "string"

    (proposal,) = propose_composite_keys(spec)

    assert proposal.score == round(COMPOSITE_SCORE / 2, 2)
    assert "LineNo" in proposal.reason
    assert "string vs bigint" in proposal.reason


def test_a_composite_key_table_does_not_reference_itself() -> None:
    line = _keyed(
        "OrderLine",
        _col("OrderId"),
        _col("LineNo"),
        key=["OrderId", "LineNo"],
    )

    assert propose_composite_keys(_spec(line)) == []


def test_a_single_column_key_is_left_to_the_name_matcher() -> None:
    owner = _keyed("Owner", _col("OwnerId"), key=["OwnerId"])
    widget = _keyed(
        "Widget", _col("WidgetId"), _col("OwnerId"), key=["WidgetId"]
    )

    assert propose_composite_keys(_spec(owner, widget)) == []


# --- what the statistics rule out ------------------------------------------


def test_a_child_reaching_past_the_parents_range_is_ruled_out() -> None:
    """One value outside the parent's range is one the parent cannot have."""
    owner = _keyed("Owner", _ranged("OwnerId", 1, 100), key=["OwnerId"])
    widget = _keyed(
        "Widget",
        _ranged("WidgetId", 1, 9000),
        _ranged("Custodian", 1, 101),
        key=["WidgetId"],
    )

    found, _ = propose_from_statistics(_spec(owner, widget))

    assert ("dbo.Widget", "Custodian", "dbo.Owner") not in _triples(found)


def test_a_child_with_more_distinct_values_than_the_parent_is_ruled_out() -> (
    None
):
    """More values than the parent has keys is not a containment failure to
    check; it is one already proved."""
    owner = _keyed("Owner", _ranged("OwnerId", 1, 100, 40), key=["OwnerId"])
    widget = _keyed(
        "Widget",
        _ranged("WidgetId", 1, 9000, 9000),
        _ranged("Custodian", 1, 100, 41),
        key=["WidgetId"],
    )
    spec = _spec(owner, widget)
    pair = ("dbo.Widget", "Custodian", "dbo.Owner")

    assert pair not in _triples(propose_from_statistics(spec)[0])

    # And it was the count that did it: the ranges alone allow the pair.
    for column in (owner.columns[0], widget.columns[1]):
        column.observed_distinct = None
    assert pair in _triples(propose_from_statistics(spec)[0])


def test_a_pair_no_statistic_touches_is_not_a_discovery() -> None:
    """Nothing was tested, so nothing was found - and nothing is counted.

    Counting an untested pair as pruned would make a spec nobody profiled
    look like one where the statistics ruled everything out.
    """
    owner = _keyed("Owner", _col("OwnerId"), key=["OwnerId"])
    widget = _keyed(
        "Widget", _col("WidgetId"), _col("Custodian"), key=["WidgetId"]
    )

    found, report = propose_from_statistics(_spec(owner, widget))

    assert found == []
    assert report.pairs == 0
    assert report.survivors == 0


def test_a_type_that_disagrees_is_never_a_pair() -> None:
    owner = _keyed("Owner", _ranged("OwnerId", 1, 100, 40), key=["OwnerId"])
    widget = _keyed(
        "Widget",
        _col("WidgetId", "string", observed_distinct=10),
        key=["WidgetId"],
    )

    _, report = propose_from_statistics(_spec(owner, widget))

    assert report.pairs == 0


def test_a_column_is_not_proposed_as_a_reference_to_itself() -> None:
    owner = _keyed("Owner", _ranged("OwnerId", 1, 100, 40), key=["OwnerId"])

    found, report = propose_from_statistics(_spec(owner))

    assert found == []
    assert report.pairs == 0


def test_a_key_the_data_rejected_is_not_a_target() -> None:
    """A column that is not unique is not something to point at."""
    owner = _keyed("Owner", _ranged("OwnerId", 1, 512, 512), key=["OwnerId"])
    owner.primary_key_verified = False
    widget = _keyed(
        "Widget",
        _ranged("WidgetId", 1, 9000, 9000),
        _ranged("Custodian", 3, 498, 430),
        key=["WidgetId"],
    )

    found, _ = propose_from_statistics(_spec(owner, widget))

    assert "dbo.Owner" not in [f.referred_table for f in found]


def test_a_column_a_name_already_argued_for_is_left_alone() -> None:
    """A data-only rival to a name match is noise in front of the reader."""
    spec = _catalog()
    claimed: set[tuple[str, tuple[str, ...]]] = {
        ("dbo.Widget", ("Custodian",))
    }

    found, _ = propose_from_statistics(spec, claimed=claimed)

    assert all(f.columns != ["Custodian"] for f in found)


# --- the shapes names cannot reach -----------------------------------------


def test_a_table_can_reference_another_through_its_own_key() -> None:
    """An identifying relationship: the child's key is also the reference."""
    base = _keyed(
        "Widget", _ranged("WidgetId", 1, 9000, 9000), key=["WidgetId"]
    )
    extra = _keyed(
        "WidgetDetail", _ranged("DetailId", 1, 40, 40), key=["DetailId"]
    )

    found, _ = propose_from_statistics(_spec(base, extra))

    assert ("dbo.WidgetDetail", ["DetailId"], "dbo.Widget") in [
        (f.table, f.columns, f.referred_table) for f in found
    ]


def test_a_self_reference_is_proposed() -> None:
    widget = _keyed(
        "Widget",
        _ranged("WidgetId", 1, 9000, 9000),
        _ranged("Parent", 1, 8000, 300),
        key=["WidgetId"],
    )

    found, _ = propose_from_statistics(_spec(widget))

    assert ("dbo.Widget", ["Parent"], "dbo.Widget") in [
        (f.table, f.columns, f.referred_table) for f in found
    ]


# --- ranking and the cap ---------------------------------------------------


def test_the_cap_keeps_the_pairs_that_cover_most_of_the_parents_keys() -> None:
    """Coverage is what separates a reference from a coincidence.

    Every survivor here is an integer inside a wider integer range. The two
    that use most of their parent's key space are the two planted ones.
    """
    found, report = propose_from_statistics(_catalog(), limit=2)

    assert [(f.table, f.columns[0], f.referred_table) for f in found] == [
        ("dbo.Widget", "Custodian", "dbo.Owner"),
        ("dbo.Widget", "Flag", "dbo.Status"),
    ]
    assert report.survivors > report.proposed == 2


def test_the_report_counts_what_was_weighed_and_what_survived() -> None:
    """Whether pruning is selective enough is a question about a catalog,
    and these are the numbers that answer it."""
    _, report = propose_from_statistics(_catalog(), limit=2)

    assert report.pairs == 12
    assert report.survivors == 8
    assert report.proposed == 2


# --- what a discovery is worth ---------------------------------------------


def test_a_discovery_stays_below_the_threshold_after_the_data_agrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Containment is not a name. A discovery needs a person either way."""
    spec = _catalog()
    proposal = FKProposal(
        table="dbo.Widget",
        columns=["Custodian"],
        referred_table="dbo.Owner",
        referred_columns=["OwnerId"],
        score=DISCOVERY_SCORE,
        reason="no name evidence",
        basis="data",
    )
    monkeypatch.setattr(
        "stele.infer._one",
        lambda engine, stmt: (
            {"distinct_values": 1000, "matched_values": 1000}
            if "distinct_values" in str(stmt)
            else {"total": 1000, "nulls": 0}
        ),
    )

    validate_foreign_key(object(), spec, proposal)  # type: ignore[arg-type]

    assert proposal.containment == 1.0
    assert proposal.score < DEFAULT_MIN_SCORE


def _discovery(**over: Any) -> FKProposal:
    return FKProposal(
        table="dbo.Widget",
        columns=["Custodian"],
        referred_table="dbo.Owner",
        referred_columns=["OwnerId"],
        score=DISCOVERY_SCORE,
        reason="no name evidence",
        basis="data",
        **over,
    )


def test_the_overlay_calls_a_discovery_discovered(tmp_path: Path) -> None:
    """Nothing argued against it; it has no name behind it."""
    text = _stub(tmp_path, _discovery())

    assert "DISCOVERED" in text
    assert "REJECTED" not in text


def test_the_overlay_calls_a_contradicted_discovery_rejected(
    tmp_path: Path,
) -> None:
    """The data was read, and it argued against this one."""
    text = _stub(tmp_path, _discovery(distinct_values=100, matched_values=12))

    assert "REJECTED" in text
    assert "DISCOVERED" not in text


def test_a_name_proposal_below_the_threshold_is_still_rejected(
    tmp_path: Path,
) -> None:
    text = _stub(
        tmp_path,
        FKProposal(
            table="dbo.Widget",
            columns=["OwnerId"],
            referred_table="dbo.Owner",
            referred_columns=["OwnerId"],
            score=0.4,
            reason="name match but type differs",
        ),
    )

    assert "REJECTED" in text
    assert "DISCOVERED" not in text


# --- wiring ----------------------------------------------------------------


def test_discovery_is_off_unless_asked_for() -> None:
    """It costs queries to check and attention to judge."""
    result = infer(_catalog())

    assert result.discovery is None
    assert result.foreign_keys == []


def test_asking_for_discovery_reports_what_it_did() -> None:
    result = infer(_catalog(), discover=True, max_discoveries=2)

    assert result.discovery is not None
    assert result.discovery.proposed == 2
    assert all(f.basis == "data" for f in result.foreign_keys)


def test_a_spec_nobody_profiled_has_nothing_to_prune_with() -> None:
    owner = _keyed("Owner", _col("OwnerId"), key=["OwnerId"])

    assert not has_statistics(_spec(owner))
    assert has_statistics(_catalog())
