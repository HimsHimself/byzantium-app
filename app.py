import os
import psycopg2
from flask import Flask

app = Flask(__name__)

# Default route
@app.route('/')
def hello():
    return "Hello from Byzantium! The web service is running."

# New database test route
@app.route('/db_test')
def db_test():
    conn = None
    try:
        # Get the database URL from the environment variable
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            return "DATABASE_URL environment variable is not set.", 500

        # Establish the connection
        conn = psycopg2.connect(db_url)
        # Create a cursor
        cur = conn.cursor()
        # Execute a simple query to get the PostgreSQL version
        cur.execute("SELECT version();")
        # Fetch the result
        db_version = cur.fetchone()
        # Close the cursor
        cur.close()
        # Return a success message with the version
        return f"Database connection successful!<br/>PostgreSQL version: {db_version[0]}"

    except Exception as e:
        # Return an error message if anything goes wrong
        return f"Database connection failed: {e}", 500
    finally:
        # Make sure to close the connection
        if conn:
            conn.close()