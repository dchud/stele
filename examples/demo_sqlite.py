"""End-to-end demo with no Databricks connection required.

Builds a small spec by hand (standing in for `stele introspect`), runs the
inference and generation pipeline, then exercises the generated models against
an in-memory SQLite database. Useful for seeing the shape of the output and for
checking changes to the generator.

    python examples/demo_sqlite.py
"""

from __future__ import annotations

import datetime as dt
import importlib
import shutil
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, configure_mappers

from stele.generate import generate
from stele.infer import infer
from stele.overlay import apply_overlay, load_overlay, write_overlay_stub
from stele.introspect import pair_history_tables
from stele.spec import ColumnSpec, HistoryConfig, ModelSpec, TableSpec


def col(name, type_, nullable=True, ordinal=0):
    return ColumnSpec(name=name, source_type=type_, nullable=nullable, ordinal=ordinal)


def build_spec() -> ModelSpec:
    """Stands in for the output of `stele introspect` against a federated catalog.

    Note what is deliberately absent: no primary keys, no foreign keys, and
    every character column reported as bare `string`. That is what federation
    actually gives you.
    """
    base_customer = [
        col("CustomerId", "bigint", nullable=False, ordinal=1),
        col("CustomerName", "string", ordinal=2),
        col("RegionId", "bigint", ordinal=3),
        col("CreditLimit", "decimal(18,2)", ordinal=4),
        col("IsActive", "boolean", ordinal=5),
    ]
    base_region = [
        col("RegionId", "bigint", nullable=False, ordinal=1),
        col("RegionName", "string", ordinal=2),
    ]
    base_order = [
        col("OrderId", "bigint", nullable=False, ordinal=1),
        col("CustomerId", "bigint", ordinal=2),
        col("OrderDate", "timestamp_ntz", ordinal=3),
        col("Amount", "decimal(18,2)", ordinal=4),
        col("Notes", "string", ordinal=5),
    ]

    def with_interval(cols):
        n = len(cols)
        return [
            *[ColumnSpec(**{**c.__dict__}) for c in cols],
            col("StartDate", "timestamp_ntz", nullable=False, ordinal=n + 1),
            col("EndDate", "timestamp_ntz", ordinal=n + 2),
        ]

    tables = [
        TableSpec(name="Customer", schema="dbo", columns=base_customer),
        TableSpec(name="Customer_history", schema="dbo", columns=with_interval(base_customer)),
        TableSpec(name="Region", schema="dbo", columns=base_region),
        TableSpec(name="Region_history", schema="dbo", columns=with_interval(base_region)),
        TableSpec(name="Order", schema="dbo", columns=base_order),
        TableSpec(name="Order_history", schema="dbo", columns=with_interval(base_order)),
    ]
    spec = ModelSpec(
        catalog="demo_catalog",
        schemas=["dbo"],
        history=HistoryConfig(),
        tables=tables,
        source="demo (no live connection)",
    )
    pair_history_tables(spec)
    return spec


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="stele-demo-"))
    print(f"working in {workdir}\n")

    spec = build_spec()
    print("1. spec built (stands in for `stele introspect`)")
    print(f"   {len(spec.tables)} tables, {len(spec.history_tables)} history")
    print(f"   declared PKs: {sum(1 for t in spec.tables if t.primary_key)}")
    print(f"   declared FKs: {sum(len(t.foreign_keys) for t in spec.tables)}\n")

    # -- infer (no --validate here; no data to check against yet) ----------
    result = infer(spec, engine=None, validate=False, min_score=0.6)
    print("2. inference")
    for p in result.primary_keys:
        print(f"   PK  {p.table}({', '.join(p.columns)})  {p.score:.2f}  {p.reason}")
    for f in result.foreign_keys:
        print(f"   FK  {f.table}({', '.join(f.columns)}) -> {f.referred_table}  {f.score:.2f}")
    print()

    overlay_path = workdir / "overlay.yaml"
    write_overlay_stub(spec, result.primary_keys, result.foreign_keys, overlay_path, min_score=0.6)
    print(f"3. overlay written to {overlay_path.name} (normally you'd edit this)\n")

    # Re-load a clean spec and apply the overlay, as `stele generate` does.
    spec = build_spec()
    changes = apply_overlay(spec, load_overlay(overlay_path))
    pair_history_tables(spec)
    print(f"4. overlay applied: {len(changes)} change(s)\n")

    # Stand in for `stele profile`: federation reported bare `string`, so
    # without observed lengths every one of these becomes NVARCHAR(MAX).
    for tname, lengths in {
        "Customer": {"CustomerName": 43},
        "Customer_history": {"CustomerName": 43},
        "Region": {"RegionName": 12},
        "Region_history": {"RegionName": 12},
    }.items():
        tbl = spec.table(f"dbo.{tname}")
        for cname, n in lengths.items():
            c = tbl.column(cname)
            if c:
                c.observed_max_length = n
    print("4b. profiled string lengths applied (stands in for `stele profile`)\n")

    outdir = workdir / "models"
    report = generate(spec, outdir)
    print(f"5. generated {len(report.classes)} classes into {outdir.name}/")
    for m in report.modules:
        print(f"   {m}.py")
    if report.tables_without_pk:
        print(f"   ! no PK: {report.tables_without_pk}")
    print()

    print("--- generated customer.py (excerpt) ---")
    text = (outdir / "customer.py").read_text()
    print("\n".join(text.splitlines()[:60]))
    print("--- end excerpt ---\n")

    # -- import and exercise ----------------------------------------------
    sys.path.insert(0, str(workdir))
    models = importlib.import_module("models")
    configure_mappers()
    print(f"6. mappers configured: {len(models.metadata.tables)} tables\n")

    engine = create_engine("sqlite://").execution_options(
        schema_translate_map={models.SCHEMA_DBO: None}
    )
    models.metadata.create_all(engine)

    Customer = models.Customer
    CustomerHistory = models.CustomerHistory
    Region = models.Region

    with Session(engine) as s:
        s.add(Region(RegionId=1, RegionName="East"))
        s.add(Customer(CustomerId=10, CustomerName="Acme Ltd", RegionId=1, IsActive=True))
        for start, end, name in [
            (dt.datetime(2026, 1, 1), dt.datetime(2026, 3, 1), "Acme Inc"),
            (dt.datetime(2026, 3, 1), dt.datetime(2026, 6, 1), "Acme Co"),
            (dt.datetime(2026, 6, 1), None, "Acme Ltd"),
        ]:
            s.add(
                CustomerHistory(
                    CustomerId=10, CustomerName=name, RegionId=1, IsActive=True,
                    StartDate=start, EndDate=end,
                )
            )
        s.commit()

    with Session(engine) as s:
        at = dt.datetime(2026, 4, 15)
        row = s.scalars(CustomerHistory.as_of(at)).one()
        print(f"7. as_of({at:%Y-%m-%d})           -> {row.CustomerName}")

        cur = s.scalars(CustomerHistory.current()).one()
        print(f"   current()                    -> {cur.CustomerName}")

        changes_ = s.scalars(
            CustomerHistory.changes_between("2026-02-01", "2026-07-01")
        ).all()
        print(f"   changes_between(Feb, Jul)    -> {[c.CustomerName for c in changes_]}")

        cust = s.get(Customer, 10)
        print(f"   customer.history             -> {[h.CustomerName for h in cust.history]}")
        print(f"   customer.region              -> {cust.region.RegionName}")

        versions = s.scalars(CustomerHistory.versions_of(cust)).all()
        print(f"   versions_of(customer)        -> {len(versions)} versions")

        n_orders = s.scalars(select(Region).where(Region.RegionId == 1)).one()
        print(f"   region.customers             -> {[c.CustomerName for c in n_orders.customers]}")
    print()

    # -- replica DDL -------------------------------------------------------
    from stele.runtime import replica_ddl

    sql = replica_ddl(models.metadata, dialect_name="mssql", schemas={"dbo": "dbo"})
    print("8. SQL Server DDL (excerpt)")
    print("\n".join(sql.splitlines()[:14]))
    print("   ...\n")

    print(f"artifacts left in {workdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
