import os
import psycopg2
from flask import Flask, request # Import the 'request' object from Flask

app = Flask(__name__)

def get_db_connection():
    """Helper function to create a database connection."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise Exception("DATABASE_URL is not set")
    return psycopg2.connect(db_url)

# This function will run BEFORE every request to your site
@app.before_request
def log_visitor():
    conn = None
    try:
        # Get visitor's IP and User-Agent string
        ip_address = request.remote_addr
        user_agent = request.headers.get('User-Agent')

        # SQL command to insert the new visitor record
        sql = "INSERT INTO visitors (ip_address, user_agent) VALUES (%s, %s);"

        # Connect to the database and insert the data
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(sql, (ip_address, user_agent))
        conn.commit()
        cur.close()

    except Exception as e:
        # If logging fails for any reason, print an error to the Render logs
        # but don't crash the main application.
        print(f"Error logging visitor: {e}")
    finally:
        if conn:
            conn.close()

# --- Your existing routes ---

@app.route('/')
def hello():
    return "Hello from Byzantium! The web service is running and you have been logged."

@app.route('/db_test')
def db_test():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT version();")
        db_version = cur.fetchone()
        cur.close()
        return f"Database connection successful!<br/>PostgreSQL version: {db_version[0]}"
    except Exception as e:
        return f"Database connection failed: {e}", 500
    finally:
        if conn:
            conn.close()