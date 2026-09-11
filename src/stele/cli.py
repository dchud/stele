"""stele command line.

Pipeline:

    stele introspect  ->  model.yaml        (regenerable, disposable)
    stele profile     ->  model.yaml        (adds observed string lengths)
    stele infer       ->  overlay.yaml      (proposals + evidence, editable)
    stele generate    ->  models/           (regenerable, never hand-edited)
    stele ddl         ->  replica.sql       (SQL Server CREATE TABLE)
    stele dictionary  ->  dictionary.json   (a tbls document, for tbls doc)
    stele site        ->  mkdocs.yml + nav  (a docs project around it)
"""

from __future__ import annotations

import argparse
import importlib
import keyword
import logging
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from dotenv import find_dotenv, load_dotenv
from sqlalchemy import Engine
from sqlalchemy.orm import configure_mappers

from .apidocs import write as write_apidocs
from .db import (
    HOST_VARS,
    ConfigurationError,
    DatabricksConfig,
    databricks_engine,
)
from .dictionary import write as write_dictionary
from .generate import generate as run_generate
from .infer import (
    DEFAULT_MAX_DISCOVERIES,
    apply_to_spec,
    composite_key_tables,
    has_statistics,
    validate_declared,
)
from .infer import infer as run_infer
from .introspect import (
    diff_columns,
    pair_history_tables,
)
from .introspect import (
    introspect as run_introspect,
)
from .overlay import apply_overlay, load_overlay, write_overlay_stub
from .profile import (
    ProfileAborted,
    ProfileReport,
    profile_spec,
    profile_warnings,
    tables_to_profile,
)
from .progress import Progress
from .runtime import replica_ddl
from .scaffold import (
    API_SUBDIR,
    DOCS_SUBDIR,
    ROOT_NAV_PATH,
    read_document,
    scaffold,
)
from .spec import DEFAULT_MIN_SCORE, HistoryConfig, dump_spec, load_spec
from .tables import schema_translation

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


def _config(
    args: argparse.Namespace, *, catalog: str | None = None
) -> DatabricksConfig:
    """Resolve connection settings, `catalog` standing behind ``--catalog``.

    A spec records the catalog it describes, so a command that reads one has
    a better default than the environment's.
    """
    try:
        return DatabricksConfig.from_env(
            catalog=args.catalog or catalog,
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


def _engine(
    cfg: DatabricksConfig,
    *,
    schema_translate_map: dict[str | None, str] | None = None,
) -> Engine:
    return databricks_engine(
        cfg, readonly=True, schema_translate_map=schema_translate_map
    )


def _import_package(path_str: str) -> Any:
    """Import a generated package by its directory.

    The directory's basename is the import name, so a directory Python
    cannot name is a directory this cannot load. Saying which, and why,
    beats a ModuleNotFoundError naming something the user never typed.
    """

    path = Path(path_str).resolve()
    if not path.is_dir():
        raise SystemExit(f"{path_str}: not a directory")
    name = path.name
    if not name.isidentifier() or keyword.iskeyword(name):
        raise SystemExit(
            f"{path_str}: '{name}' is not a usable Python module name, so "
            "the package cannot be imported.\n"
            "Rename the directory, or regenerate into one whose name is a "
            "valid identifier."
        )
    sys.path.insert(0, str(path.parent))
    return importlib.import_module(name)


def _schema_map(values: list[str] | None) -> dict[str, str] | None:
    """Parse ``logical=real`` pairs, naming what is wrong when one is not."""
    if not values:
        return None
    out: dict[str, str] = {}
    for v in values:
        logical, sep, real = v.partition("=")
        if not sep or not logical:
            raise SystemExit(
                f"--schema expects logical=real, got {v!r}.\n"
                "For example: --schema dbo=dbo sales=Sales"
            )
        out[logical] = real
    return out


def _history_config(args: argparse.Namespace) -> HistoryConfig:
    return HistoryConfig(
        suffix=args.history_suffix,
        start_column=args.start_column,
        end_column=args.end_column,
        end_open=args.end_open,
        end_sentinel=args.end_sentinel,
        interval=args.interval,
        current_row_in_history=not args.current_not_in_history,
        naive_utc=not args.tz_aware,
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
    path = Path(args.spec)
    todo, skipped = tables_to_profile(
        spec, resume=args.resume, include_distinct=args.distinct
    )
    if skipped:
        print(
            f"resuming: {len(skipped)} of {len(skipped) + len(todo)} "
            f"table(s) already carry these observations, "
            f"{len(todo)} to do"
        )
    if not todo:
        print("nothing left to profile")
        return 0

    engine = _engine(
        _config(args, catalog=spec.catalog),
        schema_translate_map=schema_translation(spec),
    )
    report = ProfileReport()
    progress = Progress(len(todo))
    status = 0
    try:
        profile_spec(
            spec,
            engine,
            sample=args.sample,
            include_distinct=args.distinct,
            resume=args.resume,
            checkpoint=lambda: dump_spec(spec, path),
            progress=progress,
            report=report,
        )
    except ProfileAborted as exc:
        print(f"\nstopped: {exc}")
        status = 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        status = 130
    finally:
        progress.finish()
        # Whatever landed is worth keeping, and `--resume` reads it back.
        dump_spec(spec, path)

    print(f"profiled {len(report.counts)} table(s); updated {args.spec}")
    if report.failed:
        print(
            f"  {len(report.failed)} table(s) failed and kept whatever they "
            f"had: {', '.join(report.failed[:6])}"
            + (" ..." if len(report.failed) > 6 else "")
        )
    if status or report.failed:
        print("  -> re-run with --resume to pick up where this stopped")
    for w in profile_warnings(spec):
        print(f"  ! {w}")
    return status


def cmd_infer(args: argparse.Namespace) -> int:
    spec = load_spec(Path(args.spec))
    if args.overlay:
        changes = apply_overlay(spec, load_overlay(Path(args.overlay)))
        print(f"overlay applied: {len(changes)} change(s)")
        pair_history_tables(spec)
    if args.discover and not has_statistics(spec):
        raise SystemExit(
            f"--discover prunes with the statistics `stele profile` "
            f"records, and {args.spec} carries none.\n"
            f"Run `stele profile --spec {args.spec}`, adding --distinct if "
            "the keys are strings: only integer columns record a range."
        )
    engine = (
        _engine(
            _config(args, catalog=spec.catalog),
            schema_translate_map=schema_translation(spec),
        )
        if args.validate
        else None
    )
    result = run_infer(
        spec,
        engine,
        validate=args.validate,
        sample=args.sample,
        min_score=args.min_score,
        discover=args.discover,
        max_discoveries=args.max_discoveries,
        progress=Progress,
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

    if engine is not None:
        declared = validate_declared(spec, engine, sample=args.sample)
        if declared:
            print(f"\ndeclared references checked: {len(declared)}")
            for d in declared:
                cont = (
                    f"{d.containment:.3f}"
                    if d.containment is not None
                    else "n/a"
                )
                print(
                    f"  {d.table}({', '.join(d.columns)}) -> "
                    f"{d.referred_table}  containment={cont}"
                )
                if d.orphan_examples:
                    print(f"      unmatched: {', '.join(d.orphan_examples)}")

    if result.discovery is not None:
        found = result.discovery
        print(
            f"\ndiscovery: {found.pairs} pair(s) the statistics could "
            f"test, {found.survivors} not ruled out, "
            f"{found.proposed} proposed"
        )
        if found.survivors > found.proposed:
            print(
                f"    {found.survivors - found.proposed} more survived and "
                "were left unchecked; raise --max-discoveries to see them"
            )

    targeted = {f.referred_table for f in result.foreign_keys}
    unreached = [
        (key, cols)
        for key, cols in composite_key_tables(spec)
        if key not in targeted
    ]
    if unreached:
        print(
            f"\n{len(unreached)} table(s) have composite keys no child "
            "carries by name:"
        )
        for key, cols in unreached:
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
    if args.docs:
        pages = write_apidocs(
            spec,
            Path(args.docs),
            package=Path(args.out).name,
            preserve_names=not args.snake_case,
        )
        print(
            f"wrote {len(pages.pages)} reference page(s) to {args.docs}"
            + (f"; removed {len(pages.removed)}" if pages.removed else "")
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
    if report.removed_modules:
        print(
            f"\n  removed {len(report.removed_modules)} module(s) left by a "
            "previous run:"
        )
        for m in report.removed_modules[:10]:
            print(f"    {m}")
    if report.unpaired_history:
        print(
            f"\n  {len(report.unpaired_history)} history table(s) generated "
            "nothing, having no enabled primary to hang from:"
        )
        for h in report.unpaired_history[:10]:
            print(f"    {h}")
    if report.renamed_attrs:
        print(
            f"\n  {len(report.renamed_attrs)} column(s) are addressed by a "
            "name Python allows:"
        )
        for r in report.renamed_attrs[:10]:
            print(f"    {r}")
    for w in report.warnings:
        print(f"  ! {w}")
    return 0


def cmd_ddl(args: argparse.Namespace) -> int:
    models = _import_package(args.package)
    schemas = _schema_map(args.schema) or {
        s: s for s in models.LOGICAL_SCHEMAS
    }
    sql = replica_ddl(
        models.metadata, dialect_name=args.dialect, schemas=schemas
    )
    Path(args.out).write_text(sql, encoding="utf-8")
    print(
        f"wrote {args.out} ({sql.count('CREATE TABLE')} tables, "
        f"dialect={args.dialect})"
    )
    return 0


def cmd_dictionary(args: argparse.Namespace) -> int:
    spec = load_spec(Path(args.spec))
    if args.overlay:
        changes = apply_overlay(spec, load_overlay(Path(args.overlay)))
        print(f"overlay applied: {len(changes)} change(s)")
        pair_history_tables(spec)

    out = Path(args.out)
    doc = write_dictionary(
        spec, out, include_history=args.history == "include"
    )
    print(
        f"wrote {out} ({len(doc.tables)} table(s), "
        f"{len(doc.relations or [])} relation(s))"
    )
    described = sum(1 for t in doc.tables if t.comment)
    print(f"  {described} of {len(doc.tables)} table(s) carry a description")
    if not any(c.observed_row_count is not None for c in spec.tables):
        print(
            "  ! no row counts: run `stele profile` to record them, and "
            "--distinct for the counts that say which columns enumerate"
        )
    print(f"\n  tbls doc json://{out.resolve()} dbdoc")
    return 0


def cmd_site(args: argparse.Namespace) -> int:
    """A MkDocs site around a document, needing nothing else."""
    document = Path(args.document)
    try:
        inputs = read_document(document)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    root = Path(args.out)
    report = scaffold(inputs, root)
    for name in report.written:
        print(f"wrote {root / name}")
    for name in report.kept:
        print(f"kept {root / name} as it was")

    rendered = root / "docs" / DOCS_SUBDIR
    if not report.rendered:
        print(
            f"\n  ! {rendered} holds no rendered pages yet. Run tbls first "
            "and this again after it:\n"
            f"      tbls doc --rm-dist json://{document.resolve()} "
            f"{rendered}\n"
            "    `--rm-dist` clears that directory, so a nav file written "
            "before it does not survive."
        )
    # Only what is actually missing. A second run over stele's own site
    # has nothing to say, and saying it anyway trains people to skip the
    # output.
    advice: list[str] = []
    if "mkdocs.yml" in report.kept:
        config = (root / "mkdocs.yml").read_text(encoding="utf-8")
        if "awesome-nav" not in config:
            advice.append(
                "    mkdocs.yml: add `awesome-nav` to its `plugins`, and\n"
                "      `navigation.prune` to the theme's `features`. "
                "Without the\n      first, the pages are not grouped by "
                "schema."
            )
    if str(ROOT_NAV_PATH) in report.kept:
        named = (root / ROOT_NAV_PATH).read_text(encoding="utf-8")
        wanted = [(DOCS_SUBDIR, "Data dictionary")]
        if (root / "docs" / API_SUBDIR).is_dir():
            wanted.append((API_SUBDIR, "API reference"))
        missing = [
            f"      - {title}: {sub}"
            for sub, title in wanted
            if f": {sub}" not in named
        ]
        if missing:
            entries = "\n".join(missing)
            advice.append(
                f"    {ROOT_NAV_PATH}: name the subtree where you want it,"
                f"\n      or leave it and awesome-nav appends it last:\n"
                f"{entries}"
            )
    if advice:
        print("\n  This site was already here, so it keeps its own shape.")
        for line in advice:
            print(line)
    print(f"\n  (cd {root} && uv run mkdocs serve)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Import the generated package and configure mappers, no database."""
    models = _import_package(args.package)
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
    p.add_argument(
        "--tz-aware",
        action="store_true",
        help="keep timestamps timezone-aware instead of normalising to "
        "UTC-naive",
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
    pr.add_argument(
        "--sample",
        type=int,
        help="limit rows scanned per table; not to be combined with "
        "--distinct, which would then count only within the sample",
    )
    pr.add_argument(
        "--distinct",
        action="store_true",
        help="also count how many different values each column holds. "
        "`infer --discover` needs these; nothing else does. Several "
        "times slower than a pass without it",
    )
    pr.add_argument(
        "--resume",
        action="store_true",
        help="skip tables already carrying the observations this run "
        "would make, so an interrupted pass continues where it stopped",
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
    inf.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    inf.add_argument(
        "--discover",
        action="store_true",
        help="also propose references no name reveals, pruned from the "
        "value ranges and distinct counts `stele profile` recorded",
    )
    inf.add_argument(
        "--max-discoveries",
        type=int,
        default=DEFAULT_MAX_DISCOVERIES,
        help="how many discovered candidates to check against the data "
        f"(default {DEFAULT_MAX_DISCOVERIES})",
    )
    inf.add_argument(
        "--overlay",
        help="apply this overlay first, so its declared references are "
        "checked too",
    )
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
    g.add_argument(
        "--docs",
        metavar="DIR",
        help="also write reference pages for the package there, one per "
        "module, linking each class to its table in the data dictionary",
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

    dc = sub.add_parser(
        "dictionary", help="write a tbls document describing the model"
    )
    dc.add_argument("--spec", default="model.yaml")
    dc.add_argument("--overlay")
    dc.add_argument("--out", default="dictionary.json")
    dc.add_argument(
        "--history",
        choices=["omit", "include"],
        default="omit",
        help="whether _history tables get entries of their own; either "
        "way the table they belong to names its companion "
        "(default omit)",
    )
    dc.set_defaults(func=cmd_dictionary)

    st = sub.add_parser(
        "site", help="write a MkDocs site around a rendered dictionary"
    )
    st.add_argument(
        "--document",
        default="dictionary.json",
        help="the document `stele dictionary` wrote; the only input, so "
        "this runs where the model and its credentials are not",
    )
    st.add_argument(
        "--out",
        default=".",
        metavar="DIR",
        help="the documentation project's root, which is usually not this "
        "one: point it at the repository that publishes the site",
    )
    st.set_defaults(func=cmd_site)

    c = sub.add_parser(
        "check", help="import the package and resolve all mappers"
    )
    c.add_argument("--package", default="models")
    c.set_defaults(func=cmd_check)
    return p


def configure_logging(*, verbose: bool = False) -> None:
    """Show stele's records rather than the driver's.

    ``basicConfig`` sets the level on the *root* logger, which every
    library inherits, so asking for stele at INFO asks for the Databricks
    connector at INFO too - and it narrates authentication, retries and
    every HTTP 200 it receives. Root stays at WARNING and stele's own
    logger carries the level, so a long run says what stele chose to say.

    ``--verbose`` opens the libraries back up, because when the
    connection itself is the problem their records are the ones worth
    reading.
    """
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("stele").setLevel(
        logging.DEBUG if verbose else logging.INFO
    )
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(verbose=args.verbose)
    env_file = _load_env_file()
    if env_file:
        log.debug("read %s", env_file)
    return cast(int, args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
