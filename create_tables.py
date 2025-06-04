import os
import psycopg2

# Get the database URL from the environment variable
db_url = os.environ.get("DATABASE_URL")

if not db_url:
    raise Exception("DATABASE_URL environment variable is not set.")

conn = None
try:
    # Connect to the database
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    # SQL command to create the visitors table
    # We use "IF NOT EXISTS" to prevent an error if we run it more than once.
    create_script = """
    CREATE TABLE IF NOT EXISTS visitors (
        id SERIAL PRIMARY KEY,
        ip_address VARCHAR(45),
        user_agent TEXT,
        timestamp TIMESTAMPTZ DEFAULT NOW()
    );
    """

    # Execute the script
    cur.execute(create_script)
    # Commit the changes to the database
    conn.commit()

    print("Table 'visitors' created successfully.")

    # Close the cursor
    cur.close()

except Exception as e:
    print(f"An error occurred: {e}")

finally:
    # Make sure to close the connection
    if conn:
        conn.close()