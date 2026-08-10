from sqlalchemy import create_engine, text

from app.config import get_settings

settings = get_settings()

engine = create_engine(settings.database_url)

tables = [
    "admin_users",
    "career_applications",
    "contact_messages",
    "job_openings",
    "loan_applications",
    "loan_tiers",
    "news_articles",
]

with engine.connect() as conn:
    print()
    print("Supabase PostgreSQL record counts:")
    print("=" * 40)

    total = 0

    for table in tables:
        result = conn.execute(
            text(f"SELECT COUNT(*) FROM {table}")
        )

        count = result.scalar()

        print(f"{table}: {count}")

        total += count

    print("=" * 40)
    print(f"Total records: {total}")