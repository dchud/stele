# Prior art

stele's heuristics and its file layout were derived from the problem in front
of them rather than from a survey of what already existed. This page is that
survey, written afterwards: what is solved elsewhere, what stele borrows, and
what it does differently.

Every claim here about another project was checked against that project's own
documentation or source, and each says which. Leads that could not be checked
are named as such.

## Generating SQLAlchemy models from a live database

This is well covered, and every tool in it stops at the same place.

[**sqlacodegen**](https://github.com/agronholm/sqlacodegen) is the closest
neighbour. Its README describes a tool that "reads the structure of an existing
database and generates the appropriate SQLAlchemy model code, using the
declarative style if possible", producing "declarative code that almost looks
like it was hand written". It relies on what the database declares: a table
with "no primary key constraint (which is required by SQLAlchemy for every
model class)" yields a bare `Table` rather than a mapped class, and
relationships come from existing foreign key constraints. Its README does
describe customising code generation by subclassing its generators through
entry points — that is Python someone writes and maintains, not a data file the
generator reads.

[**automap**](https://docs.sqlalchemy.org/en/20/orm/extensions/automap.html) is
SQLAlchemy's own answer. Its documentation describes runtime reflection rather
than emitted source: mapped classes appear when `Base.prepare()` runs. The
requirements are the same two. Relationships come from examining a `Table` for
`ForeignKeyConstraint` objects, and "for a table to be mapped, it must specify
a primary key" — tables without one are skipped.

[**Django's
`inspectdb`**](https://docs.djangoproject.com/en/stable/howto/legacy-databases/)
answers a different question about the output. The Django documentation calls
it "a shortcut, not as definitive model generation", and the workflow it
describes ends with "once you've cleaned up your models, name the file
`models.py`" — the generated file becomes a hand-maintained one.

A federated foreign catalog declares no keys and no references, which is
exactly the input all three degrade on: sqlacodegen emits `Table` objects,
automap maps nothing. That gap is what stele exists for, and it is the reason
stele infers rather than reflects. The second difference is the one `inspectdb`
highlights: stele's output stays regenerable because the corrections live in a
separate file, so upstream drift shows up as a diff in the introspected model
rather than as a merge into hand-edited code.

## Inferring keys and references when the catalog declares none

The formal names are **unique column combination discovery** for keys and
**inclusion dependency discovery** for references. What stele calls a proposed
primary key is a candidate unique column combination; what it calls a foreign
key proposal with containment is an approximate inclusion dependency.

[**Desbordante**](https://github.com/Desbordante/desbordante-core) is a working
implementation. Its README describes "a high-performance data profiler that is
capable of discovering and validating many different patterns in data using
various algorithms", and its pattern list carries both halves of this: "Exact
inclusion dependencies (discovery and validation)" and "Approximate inclusion
dependencies, with g′₃ metric (discovery and validation)", alongside exact and
approximate unique column combinations. It ships a Python library on PyPI.

It runs in two modes.

**Discovery** searches a set of tables for every dependency that holds.
Desbordante's examples load tables with `algo.load_data(tables=...)`, from CSV
files or pandas frames, so it reads data that is already local.

The catalog stele reads is neither. It is federated and metered: getting a
table's contents means pulling them out of the lakehouse. `stele infer` sends
statements to the warehouse instead and reads back counts — one statement for a
primary key candidate, two for a foreign key one, the second measuring the
child column's null fraction.

Discovery finds references stele's name heuristics cannot propose at all. Four
shapes produce no proposal: a reference between opaquely named columns, an
identifying relationship on a table's own key, a composite reference —
`propose_foreign_keys` indexes only single-column keys — and a self-reference,
which the same function excludes deliberately. Nothing about any of them
reaches the overlay unless an operator writes it there.

**Verification** is a separate mode. Desbordante's AIND verifier takes two
tables and the column indices of one candidate, and returns `get_error()` and
`get_violating_clusters()`: the error rate, and the values that broke it. It
verifies whatever candidate the caller names. Its
[examples](https://github.com/Desbordante/desbordante-core/blob/main/examples/basic/mining_aind.py)
define the error as "the proportion of distinct values in the dependent set
(LHS) that must be removed to satisfy the dependency on the referenced set
(RHS) completely" — the same quantity stele's containment complements, so
`get_error()` and one minus containment are the same number.

stele's `validate_foreign_key` computes the same error rate in SQL. It reports
the ratio and not the values, so `containment 0.87 - some orphans` says that
something failed and not what; the query already builds the distinct child
values as a CTE, and the orphans are one anti-join from there. It also
validates only proposals it generated in the same run: `stele infer` never
reads the overlay, so a reference declared by hand is checked against the data
by nothing, at any point in the pipeline.

On the measure, the correspondence is exact. Qingdong Su, Zhikang Wang, Zijing
Tan, and Shuai Ma. [Discovering Approximate Inclusion
Dependencies](https://www.vldb.org/pvldb/vol18/p1210-tan.pdf). PVLDB, 18(4):
1210 - 1222, 2024. doi:10.14778/3717755.3717777. It defines an approximate IND
under insertion semantics as `R₁.A ⊆ⁱ_ε R₂.B`, satisfied "if, in r₁, the
proportion of distinct values t[A] that are not present in attribute B of r₂
falls below the given threshold" — the count of distinct left-hand values
absent from the right, over the number of distinct left-hand values. stele's
containment is the same ratio counted the other way round: distinct non-null
child values *present* in the parent, over distinct non-null child values.
Containment is one minus that error rate, so stele's thresholds of 0.999 and
0.95 are ε of 0.001 and 0.05.

The paper is explicit that the threshold carries no conventional value: "ε is a
user-defined parameter that indicates the level of violations that can be
tolerated". So the measure has a standard name and a standard definition, and
the number does not.

It also names the alternative stele did not pick. The same paper proposes a
deletion-semantics definition counting *tuples* whose value is absent rather
than distinct values, and notes that one ε "has different meanings in the two
semantics" — the proportion of distinct values absent under insertion, the
proportion of tuples affected under deletion. Counting distinct values, as
stele does, measures how much of a column's vocabulary the parent covers;
counting tuples would weight a common orphan value more heavily than a rare
one. On a mirrored subset, where the usual cause of poor containment is a
parent outside the mirror rather than a data error, the distinct-value reading
measures the gap that matters.

One lead could not be checked. Metanome is the research platform these
algorithms are published against — the paper above states its method was
integrated into it — but its own [algorithm
listing](https://hpi.de/naumann/projects/repeatability/data-profiling/metanome-ind-algorithms.html)
returned HTTP 403, and its contents are unchecked.

## Recovering string widths by profiling

The data-quality world profiles strings, but not for this.

[**ydata-profiling**](https://github.com/ydataai/ydata-profiling)'s README
states a goal of "a one-line Exploratory Data Analysis (EDA) experience", with
text analysis covering "most common categories (uppercase, lowercase,
separator), scripts (Latin, Cyrillic) and blocks (ASCII, Cyrilic)". Its README
does not list length statistics among those features; what it describes
producing is an HTML or JSON report for a person to read.

[**Great
Expectations**](https://github.com/great-expectations/great_expectations)
points the other way. Its repository describes GX Core's "Expectations:
expressive and extensible unit tests for your data" — assertions about
properties you already know, checked against new data.

stele's `profile` step samples an observed maximum length and rounds it up to a
bucket in order to pick an `NVARCHAR(n)`. The rounding and the width are the
parts no report hands over: a profiler's output is read by a person, and
stele's is read by the type mapper.

## SCD2 query helpers in an ORM

The question that matters here is specific: can any of these attach to history
tables that already exist in a source system, with their own start and end
columns?

**SQLAlchemy-Continuum**: no. Its [schema
documentation](https://sqlalchemy-continuum.readthedocs.io/en/latest/schema.html)
describes version tables it creates, holding the parent's primary key plus
`transaction_id`, `end_transaction_id`, `operation_type` and the versioned
fields, where `transaction_id` "matches to the id number in the transaction_log
table". Validity is expressed as transaction identifiers into a log the
extension also owns, not as timestamps a source system wrote. Its
[configuration
documentation](https://sqlalchemy-continuum.readthedocs.io/en/latest/configuration.html)
offers `table_name`, `transaction_column_name`, `end_transaction_column_name`,
`operation_type_column_name`, `base_classes` and `strategy`, none of which
points it at an existing table.

[**SQLAlchemy-History**](https://github.com/corridor/sqlalchemy-history) is a
fork of Continuum, and says so in its README. Its README does not address
attaching to externally-managed tables.

**SQL Server's [system-versioned temporal
tables](https://learn.microsoft.com/en-us/sql/relational-databases/tables/temporal-tables)**
are the closest thing to stele's history layer, and they are a standard rather
than a library. Microsoft's documentation describes a pair of tables where "the
system manages the period of validity for each row", through two `datetime2`
period columns. It can adopt an existing table: "During temporal table
creation, you can specify an existing history table (which must be schema
compliant) or let the system create a default history table." Its listed use
cases include "Maintaining a slowly changing dimension for decision support
applications". The query surface is `FOR SYSTEM_TIME`, whose `AS OF` subclause
qualifies rows where `ValidFrom <= date_time AND ValidTo > date_time`.

That predicate is stele's. `HistoryMixin.valid_at` under the default half-open
interval is `start <= at AND (end IS NULL OR end > at)` — the same comparison,
with `NULL` admitted as an open end because a mirrored SCD2 table usually marks
the current row that way. A catalog that marks it with a maximum sentinel
instead needs no special case here: the sentinel is later than any instant
asked about, so it passes the same comparison. `end_open` is read by
`current()`, not by `valid_at`.

Why stele is not simply temporal tables: in SQL Server's design the engine owns
the history and writes to it on every modification. stele's history is written
by a source system it does not control and never writes to. The other backend
is Databricks, whose [Delta time
travel](https://docs.databricks.com/aws/en/delta/history) queries "previous
table versions based on timestamp or table version (as recorded in the
transaction log)" — a table's snapshots rather than a row's validity — so the
same model set could not use system versioning and still address both.

`FOR SYSTEM_TIME` has five subclauses: `AS OF`, `FROM`/`TO`, `BETWEEN`,
`CONTAINED IN` and `ALL`. `as_of` implements `AS OF`. `versions_of` returns one
entity's rows, which is `ALL` narrowed to a key. `CONTAINED IN`, which selects
versions that both opened and closed inside a window, has no equivalent, and
`overlaps()` is a predicate rather than a select. `changes_between` is `start
>= a AND start < b` — versions that came into effect during a window — which
none of the five expresses.

## One model set addressing two backends

SQLAlchemy's [connections
documentation](https://docs.sqlalchemy.org/en/20/core/connections.html#translation-of-schema-names)
introduces `schema_translate_map` as support for "multi-tenancy applications
that distribute common sets of tables into multiple schemas", and describes
translating a schema name, or `None`, to a real one at execution time.

Multi-tenancy is one database with the same tables in several schemas. stele
uses the same mechanism for something the documentation does not describe: two
different database products, reached by one class hierarchy, with the schema
token resolving per engine. No generator built on the feature turned up. Of
everything on this page, this is the use furthest from what its own
documentation describes.

## Regenerate plus overlay

Half of this shape has a name.

[**Generation Gap**](https://martinfowler.com/dslCatalog/generationGap.html),
in Martin Fowler's DSL catalogue and attributed there to John Vlissides, is
defined as "Separate generated code from non-generated code by inheritance", on
the reasoning that "Generated code should never be edited by hand, otherwise
you can't safely regenerate it." That is exactly the rule stele's generated
package states in its own header, and stele follows the pattern for behaviour:
a subclass defined outside the generated package.

The overlay is a different answer to the same problem, for a different kind of
change. It is a data file that feeds the generator, so a correction about the
model's *shape* — a key the catalog never declared, a type the catalog reported
wrongly — changes what gets generated rather than being layered over it after
the fact. No established name for that shape turned up.

**dbt** was the nearest candidate and is not the same thing. Its
[documentation](https://docs.getdbt.com/reference/configs-and-properties) draws
the line as "properties declare things _about_ your project resources; configs
go the extra step of telling dbt _how_ to build those resources in your
warehouse" — properties describe models someone wrote, rather than customising
generated output.

## Where stele sits

Nothing found here does what stele does end to end. That is a statement about
the problem rather than about stele: a federated catalog that declares nothing,
mirrored SCD2 companions, and two backends answering to one model set is a
narrow combination, and each part of it is solved better elsewhere in
isolation.

Three of stele's own mechanisms have published counterparts that describe them
more precisely than stele's documentation does. Its key and reference proposals
are candidate unique column combinations and approximate inclusion
dependencies, and its containment is one minus the published error rate. Its
`as_of` computes the predicate `FOR SYSTEM_TIME AS OF` is defined by. Its
overlay is the shape Generation Gap names for behaviour, applied to model shape
instead.
