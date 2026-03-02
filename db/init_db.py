"""
Run once to create all database tables.

Usage:
    python -m db.init_db
"""
import sys

from sqlalchemy import create_engine, text

import config
from db.models import Base


def init_db() -> None:
    engine = create_engine(config.DATABASE_URL)

    # Verify connection before doing anything
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    print("Connected to database.")

    Base.metadata.create_all(engine)
    print("Tables created:")
    for table in Base.metadata.sorted_tables:
        print(f"  - {table.name}")


if __name__ == "__main__":
    try:
        init_db()
        print("Done.")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
