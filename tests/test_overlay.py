"""The overlay reader: what it accepts, what it warns about, what it adds.

`overlay.yaml` is the one file in the pipeline a human writes by hand, so a
misspelled key has to be loud and the stub `infer` writes has to be something
`generate` reads back unchanged.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy.orm import configure_mappers

from stele.generate import generate
from stele.infer import FKProposal, PKProposal
from stele.overlay import apply_overlay, load_overlay, write_overlay_stub
from stele.spec import ColumnSpec, HistoryConfig, ModelSpec, TableSpec


def _col(
    name: str, type_: str = "bigint", nullable: bool = True, ordinal: int = 0
) -> ColumnSpec:
    return ColumnSpec(
        name=name, source_type=type_, nullable=nullable, ordinal=ordinal
    )


def _spec() -> ModelSpec:
    """One introspected table carrying a reference the catalog never saw."""
    return ModelSpec(
        catalog="c",
        schemas=["dbo"],
        history=HistoryConfig(),
        tables=[
            TableSpec(
                name="Beacon",
                schema="dbo",
                columns=[
                    _col("BeaconId", "bigint", False, 1),
                    _col("BeaconName", "string", True, 2),
                    _col("SectorId", "bigint", True, 3),
                ],
            )
        ],
    )


def _beacon(spec: ModelSpec) -> TableSpec:
    tbl = spec.table("dbo.Beacon")
    assert tbl is not None
    return tbl


# -- loading --------------------------------------------------------------


def test_a_missing_overlay_loads_as_empty(tmp_path: Path) -> None:
    assert load_overlay(tmp_path / "nothing.yaml") == {}


def test_an_empty_overlay_loads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("# nothing but a comment\n", encoding="utf-8")
    assert load_overlay(path) == {}


# -- history --------------------------------------------------------------


def test_history_settings_override_the_spec() -> None:
    spec = _spec()
    changes = apply_overlay(
        spec, {"history": {"interval": "closed", "end_open": "sentinel"}}
    )

    assert spec.history.interval == "closed"
    assert spec.history.end_open == "sentinel"
    assert len(changes) == 2


def test_an_unknown_history_key_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = _spec()
    changes = apply_overlay(spec, {"history": {"end_opne": "sentinel"}})

    assert changes == []
    assert "end_opne" in caplog.text


# -- table-level keys -----------------------------------------------------


def test_table_scalars_apply() -> None:
    spec = _spec()
    apply_overlay(
        spec,
        {
            "tables": {
                "dbo.Beacon": {
                    "class_name": "Lighthouse",
                    "comment": "what the ships steer by",
                    "enabled": False,
                }
            }
        },
    )
    tbl = _beacon(spec)

    assert tbl.class_name == "Lighthouse"
    assert tbl.comment == "what the ships steer by"
    assert tbl.enabled is False


def test_a_primary_key_is_marked_manual() -> None:
    spec = _spec()
    apply_overlay(
        spec, {"tables": {"dbo.Beacon": {"primary_key": ["BeaconId"]}}}
    )
    tbl = _beacon(spec)

    assert tbl.primary_key == ["BeaconId"]
    assert tbl.primary_key_origin == "manual"


def test_an_explicit_primary_key_origin_is_kept() -> None:
    """The overlay's own origin outranks the automatic manual stamp."""
    spec = _spec()
    apply_overlay(
        spec,
        {
            "tables": {
                "dbo.Beacon": {
                    "primary_key": ["BeaconId"],
                    "primary_key_origin": "catalog",
                }
            }
        },
    )

    assert _beacon(spec).primary_key_origin == "catalog"


def test_an_unknown_table_key_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo at table level is the easiest overlay edit to get wrong."""
    spec = _spec()
    apply_overlay(
        spec,
        {
            "tables": {
                "dbo.Beacon": {
                    "class_nmae": "Lighthouse",
                    "primary_keys": ["BeaconId"],
                }
            }
        },
    )

    assert "class_nmae" in caplog.text
    assert "primary_keys" in caplog.text
    assert _beacon(spec).class_name is None


def test_the_container_keys_are_not_reported_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = _spec()
    apply_overlay(
        spec,
        {
            "tables": {
                "dbo.Beacon": {
                    "columns": {"BeaconName": {"nullable": False}},
                    "foreign_keys_mode": "merge",
                    "foreign_keys": [],
                }
            }
        },
    )

    assert "unknown table key" not in caplog.text


def test_a_table_missing_from_the_spec_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = _spec()
    changes = apply_overlay(
        spec, {"tables": {"dbo.Nowhere": {"primary_key": ["NowhereId"]}}}
    )

    assert changes == []
    assert "dbo.Nowhere" in caplog.text


# -- column-level keys ----------------------------------------------------


def test_column_corrections_apply() -> None:
    spec = _spec()
    apply_overlay(
        spec,
        {
            "tables": {
                "dbo.Beacon": {
                    "columns": {
                        "BeaconName": {
                            "type_override": "NVARCHAR(120)",
                            "nullable": False,
                            "observed_max_length": 40,
                            "comment": "printed on the chart",
                        }
                    }
                }
            }
        },
    )
    col = _beacon(spec).column("BeaconName")
    assert col is not None

    assert col.type_override == "NVARCHAR(120)"
    assert col.nullable is False
    assert col.observed_max_length == 40
    assert col.comment == "printed on the chart"


def test_a_column_missing_from_the_table_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = _spec()
    apply_overlay(
        spec,
        {
            "tables": {
                "dbo.Beacon": {"columns": {"Nowhere": {"nullable": True}}}
            }
        },
    )

    assert "Nowhere" in caplog.text


def test_an_unknown_column_key_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = _spec()
    apply_overlay(
        spec,
        {
            "tables": {
                "dbo.Beacon": {"columns": {"BeaconName": {"nulable": True}}}
            }
        },
    )

    assert "nulable" in caplog.text


# -- foreign keys ---------------------------------------------------------


def _fk(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "columns": ["SectorId"],
        "referred_table": "dbo.Sector",
        "referred_columns": ["SectorId"],
    }
    base.update(kw)
    return base


def test_foreign_keys_replace_by_default() -> None:
    spec = _spec()
    tbl = _beacon(spec)
    apply_overlay(spec, {"tables": {"dbo.Beacon": {"foreign_keys": [_fk()]}}})
    assert len(tbl.foreign_keys) == 1

    apply_overlay(
        spec,
        {
            "tables": {
                "dbo.Beacon": {
                    "foreign_keys": [
                        _fk(
                            columns=["BeaconName"],
                            referred_table="dbo.Chart",
                            referred_columns=["ChartName"],
                        )
                    ]
                }
            }
        },
    )

    assert [f.columns for f in tbl.foreign_keys] == [["BeaconName"]]


def test_foreign_keys_merge_keeps_what_is_there() -> None:
    spec = _spec()
    tbl = _beacon(spec)
    apply_overlay(spec, {"tables": {"dbo.Beacon": {"foreign_keys": [_fk()]}}})

    apply_overlay(
        spec,
        {
            "tables": {
                "dbo.Beacon": {
                    "foreign_keys_mode": "merge",
                    "foreign_keys": [
                        _fk(
                            columns=["BeaconName"],
                            referred_table="dbo.Chart",
                            referred_columns=["ChartName"],
                        )
                    ],
                }
            }
        },
    )

    assert [f.columns for f in tbl.foreign_keys] == [
        ["SectorId"],
        ["BeaconName"],
    ]


def test_merge_skips_a_reference_already_present() -> None:
    """Same columns and same target: one claim, however often it is stated."""
    spec = _spec()
    tbl = _beacon(spec)
    apply_overlay(spec, {"tables": {"dbo.Beacon": {"foreign_keys": [_fk()]}}})

    apply_overlay(
        spec,
        {
            "tables": {
                "dbo.Beacon": {
                    "foreign_keys_mode": "merge",
                    "foreign_keys": [_fk(relationship_name="sector")],
                }
            }
        },
    )

    assert len(tbl.foreign_keys) == 1
    assert tbl.foreign_keys[0].relationship_name is None


def test_a_hand_written_reference_is_manual_unless_it_says_otherwise() -> None:
    spec = _spec()
    apply_overlay(
        spec,
        {
            "tables": {
                "dbo.Beacon": {
                    "foreign_keys": [
                        _fk(),
                        _fk(
                            columns=["BeaconName"],
                            referred_table="dbo.Chart",
                            referred_columns=["ChartName"],
                            origin="inferred",
                            confidence=0.9,
                        ),
                    ]
                }
            }
        },
    )
    fks = _beacon(spec).foreign_keys

    assert fks[0].origin == "manual"
    assert fks[1].origin == "inferred"
    assert fks[1].confidence == 0.9


def test_an_unknown_foreign_key_key_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = _spec()
    apply_overlay(
        spec,
        {
            "tables": {
                "dbo.Beacon": {"foreign_keys": [_fk(refered_table="dbo.Typo")]}
            }
        },
    )

    assert "refered_table" in caplog.text
    assert len(_beacon(spec).foreign_keys) == 1


# -- tables the catalog never reported ------------------------------------


def _added() -> dict[str, Any]:
    return {
        "add_tables": {
            "dbo.Sector": {
                "primary_key": ["SectorId"],
                "columns": {
                    "SectorId": {"source_type": "bigint", "nullable": False},
                    "SectorName": {
                        "source_type": "string",
                        "comment": "as printed",
                    },
                    "Bearing": {"type_override": "Numeric(5, 2)"},
                },
            }
        }
    }


def test_an_added_table_carries_its_columns() -> None:
    spec = _spec()
    apply_overlay(spec, _added())
    tbl = spec.table("dbo.Sector")
    assert tbl is not None

    assert tbl.schema == "dbo"
    assert [c.name for c in tbl.columns] == [
        "SectorId",
        "SectorName",
        "Bearing",
    ]
    assert [c.ordinal for c in tbl.columns] == [1, 2, 3]
    assert tbl.column("SectorId").nullable is False  # type: ignore[union-attr]
    assert tbl.column("Bearing").type_override == "Numeric(5, 2)"  # type: ignore[union-attr]
    assert tbl.primary_key == ["SectorId"]


def test_an_added_column_needs_a_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = _spec()
    apply_overlay(
        spec,
        {
            "add_tables": {
                "dbo.Shoal": {"columns": {"ShoalId": {"nullable": False}}}
            }
        },
    )
    tbl = spec.table("dbo.Shoal")
    assert tbl is not None

    assert tbl.columns == []
    assert "ShoalId" in caplog.text


def test_an_added_table_without_columns_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A table with no columns cannot map, and the failure is far from here."""
    spec = _spec()
    apply_overlay(spec, {"add_tables": {"dbo.Shoal": {"class_name": "Shoal"}}})

    assert "dbo.Shoal" in caplog.text
    assert "no columns" in caplog.text


def test_an_added_table_never_displaces_an_introspected_one() -> None:
    spec = _spec()
    apply_overlay(
        spec,
        {
            "add_tables": {
                "dbo.Beacon": {"columns": {"Other": {"source_type": "bigint"}}}
            }
        },
    )

    assert len(spec.tables) == 1
    assert [c.name for c in _beacon(spec).columns] == [
        "BeaconId",
        "BeaconName",
        "SectorId",
    ]


def test_an_added_table_generates_a_class_that_maps(tmp_path: Path) -> None:
    """The round trip the feature exists for: overlay to importable class."""
    spec = _spec()
    overlay = _added()
    overlay["tables"] = {
        "dbo.Beacon": {
            "primary_key": ["BeaconId"],
            "foreign_keys": [_fk()],
        }
    }
    apply_overlay(spec, overlay)

    generate(spec, tmp_path / "seamark")
    sys.path.insert(0, str(tmp_path))
    mod = importlib.import_module("seamark")
    configure_mappers()

    assert [c.name for c in mod.Sector.__table__.columns] == [
        "SectorId",
        "SectorName",
        "Bearing",
    ]
    assert list(mod.Sector.__table__.primary_key.columns.keys()) == [
        "SectorId"
    ]
    rel = mod.Beacon.__mapper__.relationships["sector"]
    assert rel.mapper.class_ is mod.Sector


# -- the stub infer writes ------------------------------------------------


def _proposals() -> tuple[list[PKProposal], list[FKProposal]]:
    pks = [
        PKProposal(
            table="dbo.Beacon",
            columns=["BeaconId"],
            score=0.9,
            reason="unique and not null",
            total_rows=100,
            duplicate_groups=0,
            null_rows=0,
        ),
        PKProposal(
            table="dbo.Chart",
            columns=["ChartName"],
            score=0.2,
            reason="name-shaped only",
        ),
    ]
    fks = [
        FKProposal(
            table="dbo.Beacon",
            columns=["SectorId"],
            referred_table="dbo.Sector",
            referred_columns=["SectorId"],
            score=0.8,
            reason="name and type match",
            distinct_values=10,
            matched_values=10,
        ),
        FKProposal(
            table="dbo.Beacon",
            columns=["BeaconName"],
            referred_table="dbo.Chart",
            referred_columns=["ChartName"],
            score=0.1,
            reason="weak",
        ),
    ]
    return pks, fks


def test_the_stub_round_trips_through_the_reader(tmp_path: Path) -> None:
    """What `infer` writes is what `generate` reads back."""
    pks, fks = _proposals()
    path = tmp_path / "overlay.yaml"
    write_overlay_stub(ModelSpec(), pks, fks, path, min_score=0.5)

    spec = _spec()
    spec.tables.append(
        TableSpec(
            name="Sector",
            schema="dbo",
            columns=[_col("SectorId", "bigint", False, 1)],
        )
    )
    apply_overlay(spec, load_overlay(path))
    tbl = _beacon(spec)

    assert tbl.primary_key == ["BeaconId"]
    assert tbl.primary_key_origin == "manual"
    assert len(tbl.foreign_keys) == 1
    fk = tbl.foreign_keys[0]
    assert fk.columns == ["SectorId"]
    assert fk.referred_table == "dbo.Sector"
    assert fk.referred_columns == ["SectorId"]
    assert fk.origin == "inferred"
    assert fk.confidence == 0.8


def test_the_stub_leaves_rejected_proposals_commented_out(
    tmp_path: Path,
) -> None:
    pks, fks = _proposals()
    path = tmp_path / "overlay.yaml"
    write_overlay_stub(ModelSpec(), pks, fks, path, min_score=0.5)
    loaded = load_overlay(path)

    assert "dbo.Chart" not in loaded["tables"]
    assert path.read_text(encoding="utf-8").count("REJECTED") == 2


def test_the_stub_quotes_names_that_yaml_would_swallow(
    tmp_path: Path,
) -> None:
    """Nothing stops a source column from being named `Rate, %`."""
    pks = [
        PKProposal(
            table="dbo.Odd",
            columns=["Key, Composite"],
            score=0.9,
            reason="unique",
        )
    ]
    fks = [
        FKProposal(
            table="dbo.Odd",
            columns=["ref: id"],
            referred_table="dbo.Target",
            referred_columns=["[id]"],
            score=0.9,
            reason="name match",
        )
    ]
    path = tmp_path / "overlay.yaml"
    write_overlay_stub(ModelSpec(), pks, fks, path, min_score=0.5)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    entry = loaded["tables"]["dbo.Odd"]

    assert entry["primary_key"] == ["Key, Composite"]
    assert entry["foreign_keys"][0]["columns"] == ["ref: id"]
    assert entry["foreign_keys"][0]["referred_columns"] == ["[id]"]
