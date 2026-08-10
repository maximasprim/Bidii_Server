from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.admin_user import AdminUser
from app.models.career_application import CareerApplication
from app.models.contact import ContactMessage
from app.models.job_opening import JobOpening
from app.models.loan_application import LoanApplication
from app.models.loan_tier import LoanTier
from app.models.news_article import NewsArticle


# ============================================================
# DATABASE CONNECTIONS
# ============================================================

settings = get_settings()

BASE_DIR = Path(__file__).resolve().parent
sqlite_url = f"sqlite:///{BASE_DIR / 'bidii.db'}"

print("Connecting to SQLite source database...")
sqlite_engine = create_engine(
    sqlite_url,
    connect_args={"check_same_thread": False},
)

print("Connecting to Supabase PostgreSQL destination...")
postgres_engine = create_engine(settings.database_url)


SQLiteSession = sessionmaker(
    bind=sqlite_engine,
    autocommit=False,
    autoflush=False,
)

PostgresSession = sessionmaker(
    bind=postgres_engine,
    autocommit=False,
    autoflush=False,
)


# ============================================================
# MODELS
# ============================================================

# Order matters because career_applications.job_id references
# job_openings.id.
MODELS = [
    AdminUser,
    JobOpening,
    CareerApplication,
    ContactMessage,
    LoanApplication,
    LoanTier,
    NewsArticle,
]


# ============================================================
# MIGRATION
# ============================================================

def migrate():
    sqlite_db = SQLiteSession()
    postgres_db = PostgresSession()

    try:
        print()
        print("=" * 60)
        print("STARTING SQLITE → SUPABASE MIGRATION")
        print("=" * 60)

        total_migrated = 0

        for model in MODELS:
            table_name = model.__tablename__

            print()
            print(f"Migrating: {table_name}")
            print("-" * 60)

            # Read all records from SQLite
            records = sqlite_db.scalars(
                select(model)
            ).all()

            print(f"SQLite records found: {len(records)}")

            if not records:
                print("Nothing to migrate.")
                continue

            migrated = 0
            skipped = 0

            for source_record in records:

                # Get primary key
                primary_key_column = list(model.__table__.primary_key.columns)[0]
                primary_key = getattr(
                    source_record,
                    primary_key_column.name,
                )

                # Check whether this record already exists in PostgreSQL
                existing = postgres_db.get(model, primary_key)

                if existing:
                    print(
                        f"  SKIP: {table_name} "
                        f"id={primary_key} already exists"
                    )
                    skipped += 1
                    continue

                # Copy every database column exactly
                data = {}

                for column in model.__table__.columns:
                    data[column.name] = getattr(
                        source_record,
                        column.name,
                    )

                # Create a new SQLAlchemy object for PostgreSQL
                destination_record = model(**data)

                postgres_db.add(destination_record)

                migrated += 1

            # Commit this table
            postgres_db.commit()

            print(f"Migrated: {migrated}")
            print(f"Skipped:   {skipped}")

            total_migrated += migrated

        print()
        print("=" * 60)
        print("MIGRATION COMPLETED SUCCESSFULLY")
        print("=" * 60)
        print(f"Total new records migrated: {total_migrated}")

    except Exception as e:
        postgres_db.rollback()

        print()
        print("=" * 60)
        print("MIGRATION FAILED")
        print("=" * 60)
        print(f"Error: {e}")

        raise

    finally:
        sqlite_db.close()
        postgres_db.close()
        sqlite_engine.dispose()
        postgres_engine.dispose()


if __name__ == "__main__":
    migrate()