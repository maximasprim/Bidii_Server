import sqlite3

conn = sqlite3.connect("bidii.db")
cursor = conn.cursor()

tables = [
    "admin_users",
    "career_applications",
    "contact_messages",
    "job_openings",
    "loan_applications",
    "loan_tiers",
    "news_articles",
]

print("\nSQLite record counts:")
print("=" * 40)

for table in tables:
    cursor.execute(f"SELECT COUNT(*) FROM {table}")
    count = cursor.fetchone()[0]
    print(f"{table}: {count}")

print("=" * 40)

conn.close()


# import sqlite3

# conn = sqlite3.connect("bidii.db")

# cursor = conn.cursor()

# cursor.execute(
#     "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
# )

# tables = cursor.fetchall()

# print("\nSQLite tables:")
# print("-" * 30)

# for table in tables:
#     print(table[0])

# print("-" * 30)
# print(f"Total tables: {len(tables)}")

# conn.close()