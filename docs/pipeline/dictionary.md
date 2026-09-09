# dictionary

```bash
stele dictionary --spec model.yaml --overlay overlay.yaml
tbls doc json://dictionary.json ./docs/database
```

`dictionary` writes one JSON file describing the model. [tbls][tbls] reads that
file as a datasource and renders it: an index, a page per table, a page per
viewpoint, and ER diagrams. Neither half talks to a database. The catalog is
read once, by `introspect` and `profile`, and everything on the page comes out
of `model.yaml`.

That matters on a metered catalog. Documentation regenerates as often as you
like for the cost of reading a local file.

## What is on the page

For a database that declares its constraints there is little to say, and tbls
pointed at that database directly says it better. The case for this command is
the federated catalog that declares nothing, where the interesting content is
what stele worked out and what the data said about it.

Two kinds of thing survive the trip that a conventional dictionary has no room
for.

**Provenance.** Every key and every reference records where it came from and
whether the data bore it out. They reach the page as constraints:

| Name | Type | Definition |
|---|---|---|
| PK_Order | PRIMARY KEY | PRIMARY KEY (OrderId) - inferred by stele; verified against the data |
| FK_Order_CustomerId | FOREIGN KEY | FOREIGN KEY (CustomerId) REFERENCES dbo.Customer (CustomerId) - inferred by stele; column name matches the parent's key; containment 0.997 |

A reader who knows the second line was a guess reads the rest of the page
differently.

**Observed behaviour.** What `profile` measured appears in each column's Extra
Definition, and the row count at the top of the table's page:

| Name | Type | Nullable | Extra Definition |
|---|---|---|---|
| OrderId | BIGINT | false | no nulls; 1,204,331 distinct; range [1, 1,204,331] |
| CustomerId | BIGINT | true | 2.1% null; 84,220 distinct |
| Custodian | STRING | true | 11.4% null; longest observed 18 |

Those say which columns are optional in practice rather than in the catalog,
which look like enumerations, and which are far narrower than the type they
declare. Run `profile --distinct` before `dictionary` if you want the distinct
counts; only integer columns record a range without it.

## Where the prose comes from

`comment` on a table or a column is an overlay field. Write a description
there and it appears on the dictionary page and as a docstring or a column
comment in the generated package, from one source that cannot drift:

```yaml
tables:
  dbo.Order:
    comment: One row per customer order.
    columns:
      Custodian:
        comment: Free-text owner name. Superseded by OwnerId.
```

The run reports how many tables carry a description, which is the number to
watch as you fill them in.

## Labels and viewpoints

Every table stele guessed at carries a label: `inferred` for a key or
reference a heuristic proposed, `manual` for one asserted in the overlay,
`unverified` for a key the data has not confirmed, `history` for a table with
an SCD2 companion. tbls filters on them:

```bash
tbls doc --label inferred json://dictionary.json ./docs/review
```

The document also carries viewpoints, which are named subsets tbls renders as
their own pages: one per schema where the model spans several, and one called
*Inferred shape* collecting everything labelled `inferred` or `unverified`.
That page is the shortest route to reviewing what a run proposed.

## History tables

`--history omit`, the default, leaves `_history` tables out of the document.
They double the table count and repeat their primary's columns. The table they
belong to names its companion and the interval columns in its description, and
carries the `history` label.

`--history include` gives them entries of their own, with their columns and
their row counts.

## Flags worth knowing

| Flag | Effect |
|---|---|
| `--overlay` | apply an overlay first; this is where descriptions live |
| `--out` | where to write, default `dictionary.json` |
| `--history` | `omit` or `include`, default `omit` |

## Running tbls

tbls is a Go binary, installed separately:

```bash
brew install k1LoW/tap/tbls        # or see the project's own instructions
```

The DSN is the `json://` scheme followed by a path, relative or absolute:

```bash
tbls doc json://dictionary.json ./docs/database
tbls doc json:///srv/models/dictionary.json ./docs/database
```

Some of its other flags:

| Flag | Effect |
|---|---|
| `--er-format mermaid` | Mermaid diagrams rather than the default SVG |
| `--without-er` | no diagrams |
| `--label` | only tables carrying a label |
| `--rm-dist` | clear the output directory first |
| `-b, --base-url` | prefix for links, for a site rooted below `/` |

`tbls lint` checks the document against rules you configure — every table
described, every column described, no table without a comment. `tbls coverage`
reports how much of it is documented, which is a way to track description
writing over time.

## What the format cannot carry

tbls fixes its format with a [published JSON Schema][schema] that forbids
additional properties on every object, so stele's content lands in fields that
already exist. Provenance fits well. The statistics do not: there is nowhere
structured for a null fraction or a distinct count, so they are prose in a
column's Extra Definition. They read, but they do not sort, and they cannot be
compared across tables.

[tbls]: https://github.com/k1LoW/tbls
[schema]: https://github.com/k1LoW/tbls/blob/main/spec/tbls.schema.json_schema.json
