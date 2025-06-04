import os
import psycopg2
import json # For the details column if needed, though not directly used in CREATE

# Get the database URL from the environment variable
db_url = os.environ.get("DATABASE_URL")

if not db_url:
    raise Exception("DATABASE_URL environment variable is not set.")

conn = None
try:
    # Connect to the database
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    # SQL command to create the visitors table (can be removed if fully deprecated)
    create_visitors_script = """
    CREATE TABLE IF NOT EXISTS visitors (
        id SERIAL PRIMARY KEY,
        ip_address VARCHAR(45),
        user_agent TEXT,
        timestamp TIMESTAMPTZ DEFAULT NOW()
    );
    """
    cur.execute(create_visitors_script)
    print("Table 'visitors' checked/created successfully.")

    # SQL command to create the centralized activity_log table
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
    print("Table 'activity_log' created successfully.")

    # Commit the changes to the database
    conn.commit()

    # Close the cursor
    cur.close()

except Exception as e:
    print(f"An error occurred: {e}")

finally:
    # Make sure to close the connection
    if conn:
        conn.close()