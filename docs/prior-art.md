# Prior art

stele's heuristics and its file layout were derived from the problem in
front of them rather than from a survey of what already existed. This page
is that survey, written afterwards: what is solved elsewhere, what stele
borrows, and what it does differently.

Every claim here about another project was checked against that project's
own documentation or source, and each says which. Where a lead could not be
checked, the page says that rather than guessing.

## Generating SQLAlchemy models from a live database

This is well covered, and every tool in it stops at the same place.

**sqlacodegen** is the closest neighbour. Its README describes a tool that
"reads the structure of an existing database and generates the appropriate
SQLAlchemy model code, using the declarative style if possible", producing
"declarative code that almost looks like it was hand written". It relies on
what the database declares: a table with "no primary key constraint (which
is required by SQLAlchemy for every model class)" yields a bare `Table`
rather than a mapped class, and relationships come from existing foreign key
constraints. Its README does describe customising code generation by
subclassing its generators through entry points — that is Python someone
writes and maintains, not a data file the generator reads.

**automap** is SQLAlchemy's own answer. Its documentation describes runtime
reflection rather than emitted source: mapped classes appear when
`Base.prepare()` runs. The requirements are the same two. Relationships come
from examining a `Table` for `ForeignKeyConstraint` objects, and "for a table
to be mapped, it must specify a primary key" — tables without one are
skipped.

**Django's `inspectdb`** answers a different question about the output. The
Django documentation calls it "a shortcut, not as definitive model
generation", and the workflow it describes ends with "once you've cleaned up
your models, name the file `models.py`" — the generated file becomes a
hand-maintained one.

A federated foreign catalog declares no keys and no references, which is
exactly the input all three degrade on: sqlacodegen emits `Table` objects,
automap maps nothing. That gap is what stele exists for, and it is the
reason stele infers rather than reflects. The second difference is the one
`inspectdb` highlights: stele's output stays regenerable because the
corrections live in a separate file, so upstream drift shows up as a diff in
the introspected model rather than as a merge into hand-edited code.

## Inferring keys and references when the catalog declares none

This is the axis with a genuine literature, and stele should be more
explicit that it is standing next to one.

The formal names are **unique column combination discovery** for keys and
**inclusion dependency discovery** for references. What stele calls a
proposed primary key is a candidate unique column combination; what it calls
a foreign key proposal with containment is an approximate inclusion
dependency.

**Desbordante** is a working implementation. Its README describes "a
high-performance data profiler that is capable of discovering and validating
many different patterns in data using various algorithms", and lists
inclusion dependencies in "both exact and approximate versions with g′₃
metric" and unique column combinations "in exact and approximate forms with
g₁ metric". It ships a Python library on PyPI requiring Python 3.10 or
newer.

Said plainly: Desbordante solves the discovery half better than stele's name
heuristics do, because it searches the data instead of guessing from names.
Two things keep stele from handing the job over. The tables are federated,
so the data is not local and every scan is a round trip to a system that
charges for it; and stele's output is a proposal a human accepts, where a
name match plus a containment figure is evidence someone can judge, and an
exhaustive dependency search returns every relationship that happens to hold
in the data whether or not it means anything. Those are reasons to keep the
current design, not reasons the current design is better at discovery.

On the measure: stele's containment is the fraction of a child column's
distinct non-null values that appear in the parent. Containment of 1.0 is an
exact inclusion dependency; anything less is an approximate one, with error
equal to one minus the containment. Its two thresholds, 0.999 and 0.95, are
therefore error thresholds of 0.001 and 0.05. No source consulted here
offered a conventional value to compare them against — every one treats the
threshold as a parameter. The name of the measure is standard; the number is
not.

Two leads from this area could not be checked. The Metanome project is the
research platform these algorithms are published against, but its algorithm
listing returned HTTP 403, so this page makes no claim about what it
contains. The formal definition of an approximate inclusion dependency under
insertion semantics appears in the VLDB literature, but the paper is a PDF
this environment could not render, so the definition is described above in
stele's own terms rather than quoted.

## Recovering string widths by profiling

The data-quality world profiles strings, but not for this.

**ydata-profiling**'s README states a goal of "a one-line Exploratory Data
Analysis (EDA) experience", with text analysis covering "most common
categories (uppercase, lowercase, separator), scripts (Latin, Cyrillic) and
blocks (ASCII, Cyrilic)". It does not list string length statistics among
those features, and describes no output aimed at schema or DDL decisions —
the artefact is an HTML or JSON report for a person.

**Great Expectations** is aimed at the opposite direction. Its repository
describes "Expectations: expressive and extensible unit tests for your
data" — assertions about properties you already know, checked on new data,
rather than measurements of properties you do not.

So stele's `profile` step, which samples an observed maximum length and
rounds it up to a bucket in order to pick an `NVARCHAR(n)`, has no obvious
tool to hand off to. That is a narrow job, and its narrowness is probably
why.

## SCD2 query helpers in an ORM

The question that matters here is specific: can any of these attach to
history tables that already exist in a source system, with their own start
and end columns?

**SQLAlchemy-Continuum**: no. Its schema documentation describes version
tables it creates, holding the parent's primary key plus `transaction_id`,
`end_transaction_id`, `operation_type` and the versioned fields, where
`transaction_id` "matches to the id number in the transaction_log table".
Validity is expressed as transaction identifiers into a log the extension
also owns, not as timestamps a source system wrote. Its configuration
documentation offers `table_name`, `transaction_column_name`,
`end_transaction_column_name`, `operation_type_column_name`, `base_classes`
and `strategy` — all of which name the columns it generates. There is no
option that points it at an existing table.

**SQLAlchemy-History** is a fork of Continuum, and says so in its README.
That README does not address attaching to externally-managed tables either
way, so this page claims nothing about it beyond the absence.

**SQL Server's system-versioned temporal tables** are the closest thing to
stele's history layer, and they are a standard rather than a library.
Microsoft's documentation describes a pair of tables where "the system
manages the period of validity for each row", through two `datetime2` period
columns. It can adopt an existing table: "During temporal table creation,
you can specify an existing history table (which must be schema compliant)
or let the system create a default history table." Its listed use cases
include "Maintaining a slowly changing dimension for decision support
applications". The query surface is `FOR SYSTEM_TIME`, whose `AS OF`
subclause qualifies rows where `ValidFrom <= date_time AND ValidTo >
date_time`.

That predicate is stele's. `HistoryMixin.valid_at` under the default
half-open interval is `start <= at AND (end IS NULL OR end > at)` — the same
comparison, with `NULL` admitted as an open end because a mirrored SCD2
table usually marks the current row that way rather than with a maximum
sentinel. stele reads both, through `end_open`.

Why stele is not simply temporal tables: in SQL Server's design the engine
owns the history and writes to it on every modification. stele's history is
written by a source system it does not control and never writes to, and the
other backend is Databricks, which has no equivalent feature — so the same
model set could not use it and still address both. The read-side vocabulary
is worth borrowing regardless. `FOR SYSTEM_TIME`'s five subclauses — `AS
OF`, `FROM`/`TO`, `BETWEEN`, `CONTAINED IN` and `ALL` — cover the ground
`as_of`, `changes_between` and `versions_of` cover, and are the standard
names for it.

## One model set addressing two backends

SQLAlchemy's connections documentation introduces `schema_translate_map` as
support for "multi-tenancy applications that distribute common sets of
tables into multiple schemas", and describes translating a schema name, or
`None`, to a real one at execution time.

Multi-tenancy is one database with the same tables in several schemas.
stele uses the same mechanism for something the documentation does not
describe: two different database products, reached by one class hierarchy,
with the schema token resolving per engine. No generator built on the
feature turned up. This is the smallest and least researched claim on the
page, and also the one where stele's use is furthest from the documented
intent.

## Regenerate plus overlay

Half of this shape has a name.

**Generation Gap**, in Martin Fowler's DSL catalogue and attributed there to
John Vlissides, is defined as "Separate generated code from non-generated
code by inheritance", on the reasoning that "Generated code should never be
edited by hand, otherwise you can't safely regenerate it." That is exactly
the rule stele's generated package states in its own header, and stele
follows the pattern for behaviour: a subclass defined outside the generated
package.

The overlay is a different answer to the same problem, for a different kind
of change. It is a data file that feeds the generator, so a correction about
the model's *shape* — a key the catalog never declared, a type the catalog
reported wrongly — changes what gets generated rather than being layered
over it after the fact. No established name for that shape turned up.

**dbt** was the nearest candidate and is not the same thing. Its
documentation draws the line as "properties declare things _about_ your
project resources; configs go the extra step of telling dbt _how_ to build
those resources in your warehouse" — properties describe models someone
wrote, rather than customising generated output.

## What the survey changed

Three things worth acting on came out of it, and one worth saying.

- The inference vocabulary is standard and stele does not use it. "Unique
  column combination" and "approximate inclusion dependency" name what
  `stele infer` proposes, and containment is the complement of a standard
  error measure. Adopting the terms costs nothing and connects the
  heuristics page to a literature a reader may already know.
- `FOR SYSTEM_TIME`'s subclause names are the standard vocabulary for the
  SCD2 read surface, and stele's `as_of` already implements `AS OF`'s exact
  predicate.
- Desbordante is a real implementation of the discovery problem and is worth
  understanding before extending the heuristics further, even though the
  federated setting argues against depending on it.

And the thing worth saying plainly: nothing found here does what stele does
end to end, but that is not a claim to novelty. It is a narrow problem — a
federated catalog that declares nothing, mirrored SCD2 companions, and two
backends that must answer to one model set — and the parts of it are each
solved better elsewhere in isolation.
