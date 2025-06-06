import os
import psycopg2
import json

db_url = os.environ.get("DATABASE_URL")
if not db_url:
    raise Exception("DATABASE_URL environment variable is not set.")
conn = None
try:
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    # --- Existing Tables ---
    create_activity_log_script = """
    CREATE TABLE IF NOT EXISTS activity_log (
        id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL DEFAULT 0, activity_type VARCHAR(50) NOT NULL,
        ip_address VARCHAR(45), user_agent TEXT, path TEXT, details JSONB, timestamp TIMESTAMPTZ DEFAULT NOW()
    );
    """
    cur.execute(create_activity_log_script)
    print("Table 'activity_log' checked/created successfully.")

    create_folders_script = """
    CREATE TABLE IF NOT EXISTS folders (
        id SERIAL PRIMARY KEY, name VARCHAR(255) NOT NULL, parent_folder_id INTEGER,
        user_id INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW(),
        FOREIGN KEY (parent_folder_id) REFERENCES folders (id) ON DELETE CASCADE
    );
    """
    cur.execute(create_folders_script)
    print("Table 'folders' created successfully.")

    create_notes_script = """
    CREATE TABLE IF NOT EXISTS notes (
        id SERIAL PRIMARY KEY, title VARCHAR(255) NOT NULL, content TEXT, folder_id INTEGER,
        user_id INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW(),
        FOREIGN KEY (folder_id) REFERENCES folders (id) ON DELETE CASCADE
    );
    """
    cur.execute(create_notes_script)
    print("Table 'notes' created successfully.")

    # --- New Chat Tables ---
    create_chat_sessions_script = """
    CREATE TABLE IF NOT EXISTS chat_sessions (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL DEFAULT 1,
        title VARCHAR(255),
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """
    cur.execute(create_chat_sessions_script)
    print("Table 'chat_sessions' created successfully.")

    create_chat_messages_script = """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL,
        role VARCHAR(10) NOT NULL, -- 'user' or 'model'
        content TEXT NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        FOREIGN KEY (session_id) REFERENCES chat_sessions (id) ON DELETE CASCADE
    );
    """
    cur.execute(create_chat_messages_script)
    print("Table 'chat_messages' created successfully.")


    conn.commit()
    cur.close()
    print("Database schema setup complete.")
except Exception as e:
    print(f"An error occurred: {e}")
finally:
    if conn:
        conn.close()