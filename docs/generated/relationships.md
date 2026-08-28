# Relationships

Relationships come from foreign keys, and on a federated catalog every foreign
key is one you accepted rather than one the catalog declared. A relationship
missing from the generated code means a proposal nobody accepted, not a
relationship that cannot exist.

## The example on this page

Three schemas, chosen to have most of the awkward shapes in one place:

```
dbo.Customer          CustomerId, CustomerName, RegionId, ParentCustomerId
dbo.Customer_history  the same, plus StartDate and EndDate
dbo.Region            RegionId, RegionName
sales.Customer        CustomerId, CustomerName          ← same name as dbo.Customer
sales.Order           OrderId, CustomerId, RegionId
sales.OrderLine       OrderId, LineNo, Quantity         ← composite key
ops.Shipment          IdShipment, IdOrder               ← keys named the other way
```

`stele infer` on that spec, with no connection:

```
=== primary keys ===
  0.99  dbo.Customer(CustomerId)      name matches Customer plus a key affix
  0.99  dbo.Region(RegionId)          name matches Region plus a key affix
  0.99  sales.Customer(CustomerId)    name matches Customer plus a key affix
  0.99  sales.Order(OrderId)          name matches Order plus a key affix
  0.80  sales.OrderLine(OrderId)      prefix of table name plus 'id'
  0.99  ops.Shipment(IdShipment)      name matches Shipment plus a key affix

=== foreign keys ===
  0.80  dbo.Customer(RegionId) -> dbo.Region
  0.80  sales.Order(RegionId)  -> dbo.Region
  0.80  sales.Order(CustomerId) -> sales.Customer
          name matches Customer key, types agree (same schema preferred over dbo.Customer)
  0.80  ops.Shipment(IdOrder)  -> sales.Order
```

Four things in that output are worth reading closely.

**`sales.Order(CustomerId)` chose `sales.Customer`.** Both schemas have a
`Customer`, and the one in the child's own schema settled it. The rejected twin
is named in the evidence so the choice is reviewable.

**`ops.Shipment(IdOrder)` found `sales.Order` across a schema boundary.** The
name is unique, so nothing had to be preferred, and the prefix-style key name
matched as readily as a suffix-style one would have.

**`sales.OrderLine(OrderId)` is wrong.** It scored 0.80 from a genuine name
rule, and the real key is `(OrderId, LineNo)`. This is what review is for: the
heuristic cannot see that the table is a child collection. Fix it in the
overlay.

**`dbo.Customer.ParentCustomerId` produced nothing.** A self-reference named for
its role rather than its target does not match any name the matcher builds. Real
relationships of this kind are hand-written.

## What generation makes of it

```python
class Order(Base):
    __tablename__ = "Order"
    __table_args__ = {"schema": SCHEMA_SALES}

    OrderId: Mapped[int] = mapped_column(BigInteger(), primary_key=True, nullable=False)
    CustomerId: Mapped[int | None] = mapped_column(
        BigInteger(),
        ForeignKey(f"{SCHEMA_SALES}.Customer.CustomerId"),
        nullable=True,
    )
    RegionId: Mapped[int | None] = mapped_column(
        BigInteger(),
        ForeignKey(f"{SCHEMA_DBO}.Region.RegionId"),
        nullable=True,
    )

    # inferred relationship (confidence 0.8)
    customer: Mapped["Customer2 | None"] = relationship(
        "Customer2", back_populates="orders",
    )
    # inferred relationship (confidence 0.8)
    region: Mapped["Region | None"] = relationship(
        "Region", back_populates="orders",
    )
    order_lines: Mapped[list["OrderLine"]] = relationship(
        "OrderLine", back_populates="order",
    )
    shipments: Mapped[list["Shipment"]] = relationship(
        "Shipment", back_populates="order",
    )
```

`Customer2` is `sales.Customer`, renamed because `dbo.Customer` claimed
`Customer` first. Set `class_name` in the overlay for both and it becomes
readable.

The `# inferred relationship (confidence 0.8)` comments are deliberate. A
relationship that came from a name match reads differently from one you
declared, and the generated file is where that distinction is most useful.

## Naming

Both sides get a name derived from the *other* table:

- The many-to-one side is the parent table name in snake case: `customer`,
  `region`.
- The one-to-many side is the plural of the child table name: `orders`,
  `order_lines`, `shipments`.

Two foreign keys from one table to the same parent would collide, so the second
is prefixed with its column stem — `BillingCustomerId` and `ShippingCustomerId`
give `billing_customer` and `shipping_customer`.

Override either side per relationship in the overlay:

```yaml
tables:
  sales.Order:
    foreign_keys:
      - columns: [CustomerId]
        referred_table: sales.Customer
        referred_columns: [CustomerId]
        relationship_name: buyer      # Order.buyer
        backref_name: purchases       # Customer.purchases
```

## Composite keys and self-references

Neither is proposed. Both are ordinary overlay entries:

```yaml
tables:
  sales.OrderLine:
    primary_key: [OrderId, LineNo]
    foreign_keys_mode: replace
    foreign_keys:
      - columns: [OrderId]
        referred_table: sales.Order
        referred_columns: [OrderId]

  dbo.Customer:
    foreign_keys_mode: merge
    foreign_keys:
      - columns: [ParentCustomerId]
        referred_table: dbo.Customer
        referred_columns: [CustomerId]
        relationship_name: parent
        backref_name: children
```

`merge` on `dbo.Customer` keeps the inferred `RegionId` relationship and adds
the self-reference. `replace` on `sales.OrderLine` discards the wrong
single-column proposal.

Run `stele check --package models` after either edit. It resolves every mapper
without a database and catches a bad `referred_table` or a column count that
does not line up.

## Turning one off

```yaml
      - columns: [RegionId]
        referred_table: dbo.Region
        referred_columns: [RegionId]
        enabled: false
```

The column stays; the `ForeignKey` and the relationship do not.
