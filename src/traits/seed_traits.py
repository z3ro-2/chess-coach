import os
from typing import Iterable, Mapping, Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

# Assumes a SQLAlchemy ORM model like:
# class Trait(Base):
#     __tablename__ = "traits"
#     key = Column(String, unique=True, nullable=False, index=True)
#     name = Column(String, nullable=False)
#     category = Column(String, nullable=False)
#     description = Column(Text, nullable=False)
#     severity_weight = Column(Float, nullable=False, default=1.0)
from your_app.models import Trait

from src.traits.trait_catalog import TRAIT_CATALOG


def seed_traits(session: Session, canonical_traits: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    # Using ORM (instead of raw SQL upsert) for dialect portability and easier testing.
    traits = list(canonical_traits)
    if not traits:
        return {"inserted": 0, "updated": 0}

    keys = [t["key"] for t in traits]
    existing_by_key = {
        row.key: row
        for row in session.execute(select(Trait).where(Trait.key.in_(keys))).scalars()
    }

    inserted = 0
    updated = 0

    for trait in traits:
        key = trait["key"]
        severity_weight = float(trait.get("severity_weight", 1.0))

        db_row = existing_by_key.get(key)
        if db_row is None:
            session.add(
                Trait(
                    key=key,
                    name=trait["name"],
                    category=trait["category"],
                    description=trait["description"],
                    severity_weight=severity_weight,
                )
            )
            inserted += 1
            continue

        changed = False
        if db_row.description != trait["description"]:
            db_row.description = trait["description"]
            changed = True
        if float(db_row.severity_weight) != severity_weight:
            db_row.severity_weight = severity_weight
            changed = True

        if changed:
            updated += 1

    # No deletes performed by design.
    session.commit()
    return {"inserted": inserted, "updated": updated}


if __name__ == "__main__":
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required (PostgreSQL URL expected).")

    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        result = seed_traits(session, TRAIT_CATALOG)
        print(result)
