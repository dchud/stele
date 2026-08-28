"""stele command line.

Pipeline:

    stele introspect  ->  model.yaml        (regenerable, disposable)
    stele profile     ->  model.yaml        (adds observed string lengths)
    stele infer       ->  overlay.yaml      (proposals + evidence, editable)
    stele generate    ->  models/           (regenerable, never hand-edited)
    stele ddl         ->  replica.sql       (SQL Server CREATE TABLE)
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from dotenv import find_dotenv, load_dotenv
from sqlalchemy import Engine

from .db import (
    HOST_VARS,
    ConfigurationError,
    DatabricksConfig,
    databricks_engine,
)
from .generate import generate as run_generate
from .introspect import (
    diff_columns,
    pair_history_tables,
)
from .introspect import (
    introspect as run_introspect,
)
from .overlay import apply_overlay, load_overlay, write_overlay_stub
from .profile import profile_spec, profile_warnings
from .spec import HistoryConfig, dump_spec, load_spec

log = logging.getLogger("stele")


def _load_env_file() -> str | None:
    """Read a project-local ``.env``, without displacing the shell.

    ``override=False`` is what makes the precedence a single sentence: flag,
    then exported environment, then file. The search walks up from the working
    directory so an invocation from a subdirectory still finds the file at the
    top of the project.
    """
    path = find_dotenv(usecwd=True)
    if not path:
        return None
    load_dotenv(path, override=False)
    return path


def _config(args: argparse.Namespace) -> DatabricksConfig:
    try:
        return DatabricksConfig.from_env(
            catalog=args.catalog,
            schema=(args.schemas[0] if args.schemas else None),
            host=args.host,
            http_path=args.http_path,
            token=args.token,
        )
    except ConfigurationError as exc:
        raise SystemExit(
            f"{exc}\n"
            "Set them in the environment or a .env file "
            f"({' or '.join(HOST_VARS)}, DATABRICKS_HTTP_PATH, "
            "DATABRICKS_TOKEN, DATABRICKS_CATALOG), or pass "
            "--host, --http-path, --token, --catalog."
        ) from exc


def _engine(cfg: DatabricksConfig) -> Engine:
    return databricks_engine(cfg, readonly=True)


def _history_config(args: argparse.Namespace) -> HistoryConfig:
    return HistoryConfig(
        suffix=args.history_suffix,
        start_column=args.start_column,
        end_column=args.end_column,
        end_open=args.end_open,
        end_sentinel=args.end_sentinel,
        interval=args.interval,
        current_row_in_history=not args.current_not_in_history,
    )


# ---------------------------------------------------------------------------


def cmd_introspect(args: argparse.Namespace) -> int:
    cfg = _config(args)
    spec = run_introspect(
        _engine(cfg),
        catalog=cfg.catalog,
        schemas=args.schemas,
        history=_history_config(args),
        include=re.compile(args.include) if args.include else None,
        exclude=re.compile(args.exclude) if args.exclude else None,
    )
    dump_spec(spec, Path(args.out))

    n_hist = len(spec.history_tables)
    n_pk = sum(1 for t in spec.tables if t.primary_key)
    n_fk = sum(len(t.foreign_keys) for t in spec.tables)
    print(f"wrote {args.out}")
    print(f"  tables            {len(spec.tables)}  ({n_hist} history)")
    print(f"  declared PKs      {n_pk}")
    print(f"  declared FKs      {n_fk}")
    if n_fk == 0:
        print(
            "\n  No FK constraints found. Expected for federated "
            "foreign catalogs -\n"
            "  run `stele infer --validate` next to propose them from data."
        )

    drift = diff_columns(spec)
    if drift:
        print(f"\n  history/primary column drift in {len(drift)} pair(s):")
        for key, d in list(drift.items())[:10]:
            bits = [f"{k}={v}" for k, v in d.items() if v]
            print(f"    {key}: {'; '.join(bits)}")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    spec = load_spec(Path(args.spec))
    engine = _engine(_config(args))
    counts = profile_spec(
        spec, engine, sample=args.sample, include_distinct=args.distinct
    )
    dump_spec(spec, Path(args.spec))
    print(f"profiled {len(counts)} table(s); updated {args.spec}")
    for w in profile_warnings(spec):
        print(f"  ! {w}")
    return 0


def cmd_infer(args: argparse.Namespace) -> int:
    from .infer import apply_to_spec, composite_key_tables
    from .infer import infer as run_infer

    spec = load_spec(Path(args.spec))
    engine = _engine(_config(args)) if args.validate else None
    result = run_infer(
        spec,
        engine,
        validate=args.validate,
        sample=args.sample,
        min_score=args.min_score,
    )

    print(f"primary key proposals: {len(result.primary_keys)}")
    for p in result.primary_keys:
        mark = "OK " if p.score >= args.min_score else "-- "
        print(
            f"  {mark}{p.table}({', '.join(p.columns)})  "
            f"score={p.score:.2f}  {p.reason}"
        )

    print(f"\nforeign key proposals: {len(result.foreign_keys)}")
    for f in result.foreign_keys:
        mark = "OK " if f.score >= args.min_score else "-- "
        cont = f"{f.containment:.3f}" if f.containment is not None else "n/a"
        print(
            f"  {mark}{f.table}({', '.join(f.columns)}) -> {f.referred_table}"
            f"  score={f.score:.2f} containment={cont}"
        )

    composite = composite_key_tables(spec)
    if composite:
        print(
            f"\n{len(composite)} table(s) have composite keys; references "
            "to them are not proposed:"
        )
        for key, cols in composite:
            print(f"    {key} ({', '.join(cols)})")
        print("    -> declare those references in the overlay")

    if args.apply:
        n = apply_to_spec(spec, result, min_score=args.min_score)
        dump_spec(spec, Path(args.spec))
        print(f"\napplied {n} proposal(s) directly to {args.spec} (--apply)")
        return 0

    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"\n{out} exists; not overwriting. Pass --force to replace it.")
        return 1
    write_overlay_stub(
        spec,
        result.primary_keys,
        result.foreign_keys,
        out,
        min_score=args.min_score,
    )
    print(f"\nwrote {out} - review it, then `stele generate --overlay {out}`")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    spec = load_spec(Path(args.spec))
    if args.overlay:
        changes = apply_overlay(spec, load_overlay(Path(args.overlay)))
        print(f"overlay applied: {len(changes)} change(s)")
        if args.verbose:
            for c in changes:
                print(f"    {c}")
        pair_history_tables(spec)

    report = run_generate(
        spec, Path(args.out), preserve_names=not args.snake_case
    )
    print(
        f"wrote {len(report.modules)} module(s) / "
        f"{len(report.classes)} class(es) to {args.out}"
    )

    if report.tables_without_pk:
        print(
            f"\n  {len(report.tables_without_pk)} table(s) generated "
            "without a primary key:"
        )
        for t in report.tables_without_pk[:10]:
            print(f"    {t}")
        print(
            "    -> set primary_key in the overlay; ORM identity is "
            "unreliable until you do"
        )
    if report.lossy_columns:
        print(
            f"\n  {len(report.lossy_columns)} column(s) will not "
            "round-trip to SQL Server:"
        )
        for c in report.lossy_columns[:10]:
            print(f"    {c}")
    for w in report.warnings:
        print(f"  ! {w}")
    return 0


def cmd_ddl(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(args.package).resolve().parent))
    mod_name = Path(args.package).name
    import importlib

    models = importlib.import_module(mod_name)
    from .runtime import replica_ddl

    schemas = (
        dict(s.split("=", 1) for s in args.schema)
        if args.schema
        else {s: s for s in models.LOGICAL_SCHEMAS}
    )
    sql = replica_ddl(
        models.metadata, dialect_name=args.dialect, schemas=schemas
    )
    Path(args.out).write_text(sql, encoding="utf-8")
    print(
        f"wrote {args.out} ({sql.count('CREATE TABLE')} tables, "
        f"dialect={args.dialect})"
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Import the generated package and configure mappers, no database."""
    sys.path.insert(0, str(Path(args.package).resolve().parent))
    import importlib

    from sqlalchemy.orm import configure_mappers

    models = importlib.import_module(Path(args.package).name)
    configure_mappers()
    n = len(models.metadata.tables)
    print(f"OK: {n} table(s) mapped, all relationships resolve")
    return 0


# ---------------------------------------------------------------------------


def _add_conn_args(p: argparse.ArgumentParser) -> None:
    """Every one of these falls back to its environment variable."""
    p.add_argument("--host", help="Databricks workspace hostname")
    p.add_argument("--http-path", help="SQL warehouse HTTP path")
    p.add_argument(
        "--token", help="PAT; prefer DATABRICKS_TOKEN or a .env file"
    )
    p.add_argument("--catalog", help="catalog to read")


def _add_history_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--history-suffix", default="_history")
    p.add_argument("--start-column", default="StartDate")
    p.add_argument("--end-column", default="EndDate")
    p.add_argument("--end-open", choices=["null", "sentinel"], default="null")
    p.add_argument("--end-sentinel", default="9999-12-31T00:00:00")
    p.add_argument(
        "--interval", choices=["half_open", "closed"], default="half_open"
    )
    p.add_argument(
        "--current-not-in-history",
        action="store_true",
        help="the live row is NOT duplicated into the history table",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stele", description=__doc__.split("\n")[0]
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("introspect", help="read the catalog into a model spec")
    _add_conn_args(i)
    _add_history_args(i)
    i.add_argument("--schemas", nargs="+", required=True)
    i.add_argument("--include", help="regex; keep only matching table names")
    i.add_argument("--exclude", help="regex; drop matching table names")
    i.add_argument("--out", default="model.yaml")
    i.set_defaults(func=cmd_introspect)

    pr = sub.add_parser(
        "profile", help="observe string lengths and null rates"
    )
    _add_conn_args(pr)
    pr.add_argument("--spec", default="model.yaml")
    pr.add_argument("--schemas", nargs="*", default=[])
    pr.add_argument("--sample", type=int, help="limit rows scanned per table")
    pr.add_argument(
        "--distinct",
        action="store_true",
        help="also count distinct values (slow)",
    )
    pr.set_defaults(func=cmd_profile)

    inf = sub.add_parser("infer", help="propose keys and relationships")
    _add_conn_args(inf)
    inf.add_argument("--spec", default="model.yaml")
    inf.add_argument("--schemas", nargs="*", default=[])
    inf.add_argument(
        "--validate", action="store_true", help="check proposals against data"
    )
    inf.add_argument(
        "--sample", type=int, help="limit distinct values scanned in FK checks"
    )
    inf.add_argument("--min-score", type=float, default=0.6)
    inf.add_argument("--out", default="overlay.yaml")
    inf.add_argument("--force", action="store_true")
    inf.add_argument(
        "--apply",
        action="store_true",
        help="write accepted proposals straight into the spec instead "
        "of an overlay "
        "(skips human review; prefer the overlay)",
    )
    inf.set_defaults(func=cmd_infer)

    g = sub.add_parser("generate", help="write the ORM package")
    g.add_argument("--spec", default="model.yaml")
    g.add_argument("--overlay")
    g.add_argument("--out", default="models")
    g.add_argument(
        "--snake-case", action="store_true", help="snake_case attribute names"
    )
    g.set_defaults(func=cmd_generate)

    d = sub.add_parser("ddl", help="emit CREATE TABLE for the replica")
    d.add_argument(
        "--package", default="models", help="path to the generated package"
    )
    d.add_argument(
        "--dialect", default="mssql", choices=["mssql", "postgresql", "sqlite"]
    )
    d.add_argument("--schema", nargs="*", help="logical=real schema mappings")
    d.add_argument("--out", default="replica.sql")
    d.set_defaults(func=cmd_ddl)

    c = sub.add_parser(
        "check", help="import the package and resolve all mappers"
    )
    c.add_argument("--package", default="models")
    c.set_defaults(func=cmd_check)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    env_file = _load_env_file()
    if env_file:
        log.debug("read %s", env_file)
    return cast(int, args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
