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

    # Enable pgcrypto extension for gen_random_uuid()
    cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
    print("Ensured 'pgcrypto' extension exists.")

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
        guid UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
        title VARCHAR(255) NOT NULL,
        content JSONB,
        folder_id INTEGER,
        user_id INTEGER NOT NULL DEFAULT 1, -- Assuming user_id 1
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        FOREIGN KEY (folder_id) REFERENCES folders (id) ON DELETE CASCADE -- Notes deleted with folder
    );
    """
    cur.execute(create_notes_script)
    print("Table 'notes' created successfully (content field is now JSONB).")

    # SNQL: Table to store the relationship between notes (backlinks)
    create_note_references_script = """
    CREATE TABLE IF NOT EXISTS note_references (
        id SERIAL PRIMARY KEY,
        source_note_id INTEGER NOT NULL,
        target_note_id INTEGER NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        FOREIGN KEY (source_note_id) REFERENCES notes(id) ON DELETE CASCADE,
        FOREIGN KEY (target_note_id) REFERENCES notes(id) ON DELETE CASCADE,
        UNIQUE(source_note_id, target_note_id)
    );
    """
    cur.execute(create_note_references_script)
    print("Table 'note_references' for SNQL created successfully.")


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

    create_antiques_script = """
    CREATE TABLE IF NOT EXISTS antiques (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        description TEXT,
        item_type VARCHAR(100),
        period VARCHAR(100),
        provenance TEXT,
        approximate_value NUMERIC(10, 2),
        is_sellable BOOLEAN NOT NULL DEFAULT FALSE,
        image_url TEXT,
        user_id INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """
    cur.execute(create_antiques_script)
    print("Table 'antiques' created successfully.")
    
    # --- NEW: Table for Tasks ---
    create_tasks_script = """
    CREATE TABLE IF NOT EXISTS tasks (
        id SERIAL PRIMARY KEY,
        title VARCHAR(255) NOT NULL,
        is_completed BOOLEAN NOT NULL DEFAULT FALSE,
        due_date TIMESTAMPTZ,
        user_id INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """
    cur.execute(create_tasks_script)
    print("Table 'tasks' created successfully.")

    # --- NEW: Table for structured logs ---
    create_logs_script = """
    CREATE TABLE IF NOT EXISTS logs (
        id SERIAL PRIMARY KEY,
        log_type VARCHAR(50) NOT NULL,
        title VARCHAR(255),
        content TEXT,
        structured_data JSONB,
        log_time TIMESTAMPTZ NOT NULL,
        user_id INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """
    cur.execute(create_logs_script)
    print("Table 'logs' created successfully.")

    # --- NEW: Table for log attachments (e.g., photos for gardening) ---
    create_log_attachments_script = """
    CREATE TABLE IF NOT EXISTS log_attachments (
        id SERIAL PRIMARY KEY,
        log_id INTEGER NOT NULL,
        file_name TEXT NOT NULL,
        file_type VARCHAR(50),
        user_id INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        FOREIGN KEY (log_id) REFERENCES logs(id) ON DELETE CASCADE
    );
    """
    cur.execute(create_log_attachments_script)
    print("Table 'log_attachments' created successfully.")

    conn.commit()
    cur.close()
    print("Database schema setup complete.")
except Exception as e:
    print(f"An error occurred: {e}")
finally:
    if conn:
        conn.close()