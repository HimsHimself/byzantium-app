import os
import psycopg2
import json # Not directly used for table creation but good to keep if details are complex

db_url = os.environ.get("DATABASE_URL")
if not db_url:
    raise Exception("DATABASE_URL environment variable is not set.")
conn = None
try:
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    create_activity_log_script = """
    CREATE TABLE IF NOT EXISTS activity_log (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL DEFAULT 0,
        activity_type VARCHAR(50) NOT NULL,
        ip_address VARCHAR(45),
        user_agent TEXT,
        path TEXT,
        details JSONB,
        timestamp TIMESTAMPTZ DEFAULT NOW()
    );
    """
    cur.execute(create_activity_log_script)
    print("Table 'activity_log' checked/created successfully.")

    create_folders_script = """
    CREATE TABLE IF NOT EXISTS folders (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        parent_folder_id INTEGER, -- Nullable for root folders
        user_id INTEGER NOT NULL DEFAULT 1, -- Assuming user_id 1 for the primary user
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        FOREIGN KEY (parent_folder_id) REFERENCES folders (id) ON DELETE CASCADE -- Subfolders deleted with parent
    );
    """
    cur.execute(create_folders_script)
    print("Table 'folders' created successfully.")

    create_notes_script = """
    CREATE TABLE IF NOT EXISTS notes (
        id SERIAL PRIMARY KEY,
        title VARCHAR(255) NOT NULL,
        content TEXT,
        folder_id INTEGER,
        user_id INTEGER NOT NULL DEFAULT 1, -- Assuming user_id 1
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        FOREIGN KEY (folder_id) REFERENCES folders (id) ON DELETE CASCADE -- Notes deleted with folder
    );
    """
    cur.execute(create_notes_script)
    print("Table 'notes' created successfully.")

    create_food_log_script = """
    CREATE TABLE IF NOT EXISTS food_log (
        id SERIAL PRIMARY KEY,
        log_type VARCHAR(50) NOT NULL,
        description VARCHAR(255) NOT NULL,
        calories INTEGER,
        log_time TIMESTAMPTZ NOT NULL,
        user_id INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """
    cur.execute(create_food_log_script)
    print("Table 'food_log' created successfully.")

    # New table for the antiques collection
    create_antiques_script = """
    CREATE TABLE IF NOT EXISTS antiques (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        description TEXT,
        period VARCHAR(100),
        provenance TEXT,
        date_acquired DATE,
        image_url TEXT,
        user_id INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """
    cur.execute(create_antiques_script)
    print("Table 'antiques' created successfully.")


    conn.commit()
    cur.close()
    print("Database schema setup complete.")
except Exception as e:
    print(f"An error occurred: {e}")
finally:
    if conn:
        conn.close()