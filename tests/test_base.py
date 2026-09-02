"""``Base`` reads and reports attribute names, not column names.

A column is often addressed by a name that is not its own: ``--snake-case``
renames every attribute, a column named after a Python keyword gets a
trailing underscore, and a column name that is not an identifier cannot be
an attribute at all. The attribute name is the only name the class has, so
that is what ``to_dict`` and ``__repr__`` use for both the lookup and the
key.

The classes register on the shared declarative base, so their names carry a
prefix no other test uses.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stele.runtime import Base


class BaseAttrOwner(Base):
    __tablename__ = "base_attr_owner"

    OwnerId: Mapped[int] = mapped_column(primary_key=True)


class BaseAttrPart(Base):
    """Every way an attribute name can diverge from its column name."""

    __tablename__ = "base_attr_part"

    part_id: Mapped[int] = mapped_column("PartId", primary_key=True)
    part_name: Mapped[str] = mapped_column("PartName")
    class_: Mapped[str] = mapped_column("class")
    unit_price: Mapped[float | None] = mapped_column("Unit Price")
    owner_id: Mapped[int] = mapped_column(
        "OwnerId", ForeignKey("base_attr_owner.OwnerId")
    )
    owner: Mapped[BaseAttrOwner] = relationship()


def _part() -> BaseAttrPart:
    return BaseAttrPart(
        part_id=7,
        part_name="hex bolt",
        class_="fastener",
        unit_price=1.5,
        owner_id=3,
    )


def test_repr_names_the_primary_key_attribute() -> None:
    assert repr(_part()) == "<BaseAttrPart part_id=7>"


def test_to_dict_is_keyed_by_attribute_name() -> None:
    """Relationships stay out: only mapped columns are values."""
    assert _part().to_dict() == {
        "part_id": 7,
        "part_name": "hex bolt",
        "class_": "fastener",
        "unit_price": 1.5,
        "owner_id": 3,
    }


def test_to_dict_round_trips_through_the_constructor() -> None:
    part = _part()
    assert BaseAttrPart(**part.to_dict()).to_dict() == part.to_dict()


def test_include_none_drops_attributes_whose_value_is_none() -> None:
    part = BaseAttrPart(part_id=7, part_name="hex bolt", class_="fastener")
    assert part.to_dict()["unit_price"] is None
    assert part.to_dict(include_none=False) == {
        "part_id": 7,
        "part_name": "hex bolt",
        "class_": "fastener",
    }
