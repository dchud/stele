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
    CONSTRAINT [pk_Widget] PRIMARY KEY ([WidgetId]),
    CONSTRAINT [fk_Widget_OwnerId_Owner] FOREIGN KEY([OwnerId])
        REFERENCES dbo.[Owner] ([OwnerId])
);
```

Every constraint is named, from a convention on the declarative base rather
than from whatever the database would have assigned. Two runs against the same
model therefore produce the same names, which is what makes the emitted file
diffable and a constraint droppable by name.

Key columns carry no `IDENTITY`. The replica holds the source's key values, so a
bulk load writes them as they are — no `KEEPIDENTITY` and no renumbering.

`--dialect` also accepts `postgresql` and `sqlite`, which are useful for a local
test target.

### Schema mapping

A `schema_translate_map` is a compile-time argument as well as an execution
option, so `ddl` resolves the tokens where the tables are, with no connection
involved. `--schema` controls the mapping:

```bash
stele ddl --package models --schema dbo=dbo sales=Sales --out replica.sql
```

Without `--schema`, each logical schema maps to itself.
