# ddl and check

Two offline commands that operate on the generated package rather than the spec.

## check

```bash
stele check --package models
```

Imports the package and runs SQLAlchemy's `configure_mappers()` with no database
connection. It resolves every relationship, every `primaryjoin`, and every
string-referenced target class.

```
OK: 47 table(s) mapped, all relationships resolve
```

This is the fast way to catch a broken overlay. A relationship pointing at a
table you disabled, a `referred_table` with a typo, a composite key whose column
count does not match — all of them surface here in under a second rather than at
the first query.

Run it after every overlay edit.

## ddl

```bash
stele ddl --package models --dialect mssql --out replica.sql
```

Emits `CREATE TABLE` statements for the replica.

Because the models carry generic types with `mssql` variants, this produces real
`NVARCHAR(n)` and `DATETIME2(6)` DDL rather than the STRING-everywhere shape the
federated catalog reported:

```sql
CREATE TABLE dbo.[Widget] (
    [WidgetId] BIGINT NOT NULL,
    [WidgetName] NVARCHAR(50) NULL,
    [OwnerId] BIGINT NULL,
    PRIMARY KEY ([WidgetId]),
    FOREIGN KEY([OwnerId]) REFERENCES dbo.[Owner] ([OwnerId])
);
```

`--dialect` also accepts `postgresql` and `sqlite`, which are useful for a local
test target.

### Schema mapping

`schema_translate_map` is applied by the *connection* at execution time, so it
cannot resolve tokens during a standalone compile. `ddl` therefore clones the
tables into a fresh `MetaData` with real schema names before compiling, and
`--schema` controls the mapping:

```bash
stele ddl --package models --schema dbo=dbo sales=Sales --out replica.sql
```

Without `--schema`, each logical schema maps to itself.
