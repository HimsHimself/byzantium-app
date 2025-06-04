import os
import psycopg2
import json # For storing details in JSONB
from psycopg2.extras import Json # For adapting Python dicts to JSONB
from flask import Flask, request, session, redirect, url_for, render_template
from functools import wraps

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise ValueError("No SECRET_KEY set for Flask application.")

APP_PASSWORD = os.environ.get("APP_PASSWORD")
if not APP_PASSWORD:
    raise ValueError("No APP_PASSWORD set for Flask application.")

# --- Database Helper ---
def get_db_connection():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise Exception("DATABASE_URL is not set")
    return psycopg2.connect(db_url)

# --- Activity Logging Helper ---
def log_activity(activity_type, details=None):
    conn = None
    try:
        user_id = 1 if 'logged_in' in session else 0 # 1 for logged in, 0 for unauth/system
        ip_address = request.remote_addr
        user_agent = request.headers.get('User-Agent')
        path = request.path

        # Ensure details is None or a dict for JSONB
        if details is not None and not isinstance(details, dict):
            details = {"info": str(details)} # Convert non-dict details to a simple dict

        sql = """
            INSERT INTO activity_log (user_id, activity_type, ip_address, user_agent, path, details)
            VALUES (%s, %s, %s, %s, %s, %s);
        """
        conn = get_db_connection()
        cur = conn.cursor()
        # Use Json adapter for the details dictionary
        cur.execute(sql, (user_id, activity_type, ip_address, user_agent, path, Json(details) if details else None))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"Error logging activity '{activity_type}': {e}") # Log to Render console
    finally:
        if conn:
            conn.close()

# --- Authentication Decorator ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            # Log unauthorized access attempt before redirecting
            log_activity('unauthorized_access_attempt', details={"target_url": request.url})
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# --- Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        submitted_password = request.form['password']
        if submitted_password == APP_PASSWORD:
            session['logged_in'] = True
            session.permanent = True
            log_activity('login_success') # Log successful login
            next_url = request.args.get('next')
            return redirect(next_url or url_for('hello'))
        else:
            log_activity('login_failure', details={"reason": "Invalid password"}) # Log failed login
            error = 'Invalid Password. Please try again.'
    # Log pageview for GET request to login page
    # (Done by before_request_handler if not explicitly excluded)
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    if 'logged_in' in session:
        log_activity('logout') # Log logout before popping session
    session.pop('logged_in', None)
    return redirect(url_for('login'))

# This function will run BEFORE every request
@app.before_request
def before_request_handler():
    # Exclude static files and login route from automatic pageview logging here
    # as login route handles its own pageview logging logic if needed,
    # and static files don't typically need individual pageview logs.
    # Unauthorized access attempts are logged by @login_required
    if request.endpoint and request.endpoint not in ['login', 'static', 'logout']:
        if 'logged_in' in session: # Only log pageviews for authenticated users
             log_activity('pageview')

@app.route('/')
@login_required
def hello():
    return render_template('index.html')

@app.route('/db_test')
@login_required
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

# --- Route to view activity logs (for your eyes only) ---
@app.route('/admin/activity_log')
@login_required
def view_activity_log():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Fetch last 100 activities, newest first
        cur.execute("SELECT id, user_id, activity_type, ip_address, path, details, timestamp FROM activity_log ORDER BY timestamp DESC LIMIT 100")
        activities = cur.fetchall()
        cur.close()
        # Basic HTML display for now
        html_output = "<h1>Activity Log (Last 100)</h1><table border='1' style='width:100%; border-collapse: collapse; color: #D1D5DB;'>"
        html_output += "<tr style='background-color: #374151;'><th>ID</th><th>User ID</th><th>Type</th><th>IP</th><th>Path</th><th>Details</th><th>Timestamp</th></tr>"
        for activity in activities:
            html_output += "<tr>"
            for item in activity:
                html_output += f"<td style='padding: 5px; border: 1px solid #4B5563;'>{str(item)}</td>"
            html_output += "</tr>"
        html_output += "</table>"
        return html_output
    except Exception as e:
        return f"Error fetching activity log: {e}", 500
    finally:
        if conn:
            conn.close()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=True)
