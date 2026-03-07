import sqlite3

conn = sqlite3.connect('database.db')

# Rename old table
conn.execute("ALTER TABLE tasks RENAME TO tasks_old")

# Create new table with full status support
conn.execute('''
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        description TEXT NOT NULL,
        status TEXT DEFAULT 'new' CHECK(status IN ('new', 'wip', 'complete', 'canceled', 'failed')),
        work TEXT DEFAULT '',
        work_log TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
''')

# Copy all existing data
conn.execute('''
    INSERT INTO tasks (id, description, status, work, work_log, created_at, updated_at)
    SELECT id, description, status, work, work_log, created_at, updated_at FROM tasks_old
''')

conn.execute("DROP TABLE tasks_old")
conn.commit()
conn.close()
print("✅ Migration complete — 'failed' status is now fully supported!")
