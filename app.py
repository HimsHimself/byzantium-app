import os
import psycopg2
import json
from psycopg2.extras import Json
from flask import Flask, request, session, redirect, url_for, render_template
from functools import wraps

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise ValueError("No SECRET_KEY set for Flask application.")

APP_PASSWORD = os.environ.get("APP_PASSWORD")
if not APP_PASSWORD:
    raise ValueError("No APP_PASSWORD set for Flask application.")

def get_db_connection():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise Exception("DATABASE_URL is not set")
    return psycopg2.connect(db_url)

def log_activity(activity_type, details=None):
    conn = None
    try:
        user_id = 1 if 'logged_in' in session else 0
        ip_address = request.remote_addr
        user_agent = request.headers.get('User-Agent')
        path = request.path
        if details is not None and not isinstance(details, dict):
            details = {"info": str(details)}
        sql = """
            INSERT INTO activity_log (user_id, activity_type, ip_address, user_agent, path, details)
            VALUES (%s, %s, %s, %s, %s, %s);
        """
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(sql, (user_id, activity_type, ip_address, user_agent, path, Json(details) if details else None))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"Error logging activity '{activity_type}': {e}")
    finally:
        if conn:
            conn.close()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            log_activity('unauthorized_access_attempt', details={"target_url": request.url})
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        submitted_password = request.form['password']
        if submitted_password == APP_PASSWORD:
            session['logged_in'] = True
            session.permanent = True
            log_activity('login_success')
            next_url = request.args.get('next')
            return redirect(next_url or url_for('hello'))
        else:
            log_activity('login_failure', details={"reason": "Invalid password"})
            error = 'Invalid Password. Please try again.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    if 'logged_in' in session:
        log_activity('logout')
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.before_request
def before_request_handler():
    if request.endpoint and request.endpoint not in ['login', 'static', 'logout']:
        if 'logged_in' in session:
             log_activity('pageview')

@app.route('/')
@login_required
def hello():
    return render_template('index.html')

@app.route('/db_test')
@login_required
def db_test():
    # This route is just for testing, can be removed later
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

@app.route('/admin/activity_log')
@login_required
def view_activity_log():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, user_id, activity_type, ip_address, path, details, TO_CHAR(timestamp, 'YYYY-MM-DD HH24:MI:SS TZ') as formatted_timestamp FROM activity_log ORDER BY timestamp DESC LIMIT 100")
        activities_raw = cur.fetchall()
        colnames = [desc[0] for desc in cur.description]
        cur.close()

        activities = []
        for row in activities_raw:
            activities.append(dict(zip(colnames, row)))
        
        # Generate the correct URL for the 'Back to Dashboard' link
        dashboard_url = url_for('hello')

        html_output = f"""
        <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Activity Log</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style> body {{ font-family: 'Inter', sans-serif; background-color: #f8fafc; color: #334155; padding: 20px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 0.9em; }}
        th, td {{ border: 1px solid #cbd5e1; padding: 10px; text-align: left; }}
        th {{ background-color: #e2e8f0; color: #4B0082; }}
        tr:nth-child(even) {{ background-color: #f1f5f9; }}
        a {{ color: #4B0082; text-decoration: none; }} a:hover {{ text-decoration: underline; }}
        .details-json {{ max-width: 300px; overflow-x: auto; white-space: pre-wrap; background-color: #eef2ff; padding: 5px; border-radius: 4px; font-family: monospace; font-size: 0.8em;}}
        </style></head><body>
        <h1 class="text-2xl font-semibold mb-4" style="color: #4B0082;">Activity Log <span style="color: #DAA520;">&dagger;</span> (Last 100)</h1>
        <p><a href="{dashboard_url}">Back to Dashboard</a></p>
        <table><thead><tr>
        """
        for name in colnames:
            html_output += f"<th>{name.replace('_', ' ').title()}</th>"
        html_output += "</tr></thead><tbody>"

        for activity in activities:
            html_output += "<tr>"
            for col_name in colnames:
                value = activity.get(col_name)
                if col_name == 'details' and value is not None:
                    html_output += f"<td><pre class='details-json'>{json.dumps(value, indent=2)}</pre></td>"
                else:
                    html_output += f"<td>{str(value) if value is not None else ''}</td>"
            html_output += "</tr>"
        html_output += "</tbody></table></body></html>"
        return html_output
    except Exception as e:
        log_activity('error', details={"function": "view_activity_log", "error": str(e)})
        return f"Error fetching activity log: {e}", 500
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=True)
