import os
import psycopg2
import json
import requests # For making HTTP requests to the Cloud Function
import uuid
import threading
from psycopg2.extras import Json, RealDictCursor
from flask import Flask, request, session, redirect, url_for, render_template, flash, jsonify
from functools import wraps
from datetime import datetime
import traceback

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise ValueError("No SECRET_KEY set for Flask application.")

APP_PASSWORD = os.environ.get("APP_PASSWORD")
if not APP_PASSWORD:
    raise ValueError("No APP_PASSWORD set for Flask application.")

ORACLE_API_ENDPOINT_URL = os.environ.get("ORACLE_API_ENDPOINT_URL")
ORACLE_API_FUNCTION_KEY = os.environ.get("ORACLE_API_FUNCTION_KEY")

if not ORACLE_API_ENDPOINT_URL:
    print("[WARNING] ORACLE_API_ENDPOINT_URL not set. Oracle Chat functionality will be significantly impaired or disabled.")

oracle_jobs = {}

def get_db_connection():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise Exception("DATABASE_URL is not set")
    return psycopg2.connect(db_url)

# --- Activity Logging Helper ---
# MODIFIED: The function now accepts optional request context arguments.
# This allows it to be called from a background thread where the global `request` object is not available.
def log_activity(activity_type, details=None, ip_address=None, user_agent=None, path=None):
    conn = None
    cur = None
    try:
        user_id = 1 if 'logged_in' in session else 0
        
        # If context is not passed directly, try to get it from Flask's request object.
        # This makes the function usable in both request and non-request contexts.
        final_ip_address = ip_address or (request.remote_addr if request else 'N/A')
        final_user_agent = user_agent or (request.headers.get('User-Agent') if request else 'N/A')
        final_path = path or (request.path if request else 'N/A')

        if details is not None and not isinstance(details, dict):
            details = {"info": str(details)}

        sql = """
            INSERT INTO activity_log (user_id, activity_type, ip_address, user_agent, path, details)
            VALUES (%s, %s, %s, %s, %s, %s);
        """
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(sql, (user_id, activity_type, final_ip_address, final_user_agent, final_path, Json(details) if details else None))
        conn.commit()
    except Exception as e:
        # Avoids recursion if the logger itself fails inside a request context error.
        print(f"--- CRITICAL: Error logging activity '{activity_type}': {e} ---")
        # Added traceback for better debugging of the logger itself
        traceback.print_exc()
    finally:
        if conn:
            if cur: cur.close()
            conn.close()

# --- Authentication Decorator (Unchanged) ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            target_url = request.url if request else url_for('hello')
            # Now we can safely call log_activity here
            log_activity('unauthorized_access_attempt', details={"target_url": target_url})
            return redirect(url_for('login', next=target_url))
        return f(*args, **kwargs)
    return decorated_function

# --- Main Routes (Unchanged) ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        submitted_password = request.form.get('password')
        if submitted_password == APP_PASSWORD:
            session['logged_in'] = True
            session.permanent = True
            log_activity('login_success')
            next_url = request.args.get('next')
            flash('Login successful!', 'success')
            return redirect(next_url or url_for('hello'))
        else:
            log_activity('login_failure', details={"reason": "Invalid password"})
            error = 'Invalid Password. Please try again.'
            flash(error, 'error')
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    if 'logged_in' in session:
        log_activity('logout')
        session.pop('logged_in', None)
        flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.before_request
def before_request_handler():
    if 'logged_in' in session and \
       request.endpoint and \
       request.endpoint not in ['login', 'static', 'logout', 'api_oracle_chat_start', 'api_oracle_chat_status']:
        log_activity('pageview')

@app.route('/')
@login_required
def hello():
    return render_template('index.html')


# --- Notes and Folders Routes (Omitted for brevity, assumed to be unchanged) ---
# ... (Your existing code for notes_page, add_folder, etc. goes here) ...
@app.route('/notes/')
@app.route('/notes/folder/<int:folder_id>')
@login_required
def notes_page(folder_id=None):
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        current_folder = None
        folders_to_display = []
        notes_in_current_folder = []
        if folder_id:
            cur.execute("SELECT * FROM folders WHERE id = %s AND user_id = 1", (folder_id,))
            current_folder = cur.fetchone()
            if not current_folder:
                flash('Folder not found.', 'error')
                return redirect(url_for('notes_page'))
            cur.execute("SELECT * FROM folders WHERE parent_folder_id = %s AND user_id = 1 ORDER BY name", (folder_id,))
            folders_to_display = cur.fetchall()
            cur.execute("SELECT id, title, folder_id, updated_at FROM notes WHERE folder_id = %s AND user_id = 1 ORDER BY updated_at DESC", (folder_id,))
            notes_in_current_folder = cur.fetchall()
        else:
            cur.execute("SELECT * FROM folders WHERE parent_folder_id IS NULL AND user_id = 1 ORDER BY name")
            folders_to_display = cur.fetchall()
        return render_template('notes.html', folders=folders_to_display, notes_in_folder=notes_in_current_folder, current_folder=current_folder, current_note=None)
    except Exception as e:
        traceback.print_exc()
        flash("Error loading notes.", "error")
        return redirect(url_for('hello'))
    finally:
        if conn:
            if cur: cur.close()
            conn.close()

@app.route('/add_folder', methods=['POST'])
@login_required
def add_folder():
    folder_name = request.form.get('folder_name','').strip()
    parent_folder_id_str = request.form.get('parent_folder_id')
    parent_folder_id = int(parent_folder_id_str) if parent_folder_id_str and parent_folder_id_str.isdigit() else None
    redirect_url = url_for('notes_page', folder_id=parent_folder_id) if parent_folder_id else url_for('notes_page')
    if not folder_name:
        flash('Folder name cannot be empty.', 'error')
    else:
        conn = None; cur = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("INSERT INTO folders (name, parent_folder_id, user_id, created_at, updated_at) VALUES (%s, %s, 1, NOW(), NOW()) RETURNING id", (folder_name, parent_folder_id))
            new_folder_id = cur.fetchone()[0]
            conn.commit()
            log_activity('folder_created', details={'folder_name': folder_name, 'parent_id': parent_folder_id, 'new_folder_id': new_folder_id})
            flash(f"Folder '{folder_name}' created.", 'success')
            redirect_url = url_for('notes_page', folder_id=new_folder_id) # Go to new folder
        except Exception as e:
            log_activity('folder_create_error', details={'folder_name': folder_name, 'error': str(e)})
            flash(f"Error: {e}", 'error'); print(f"Error: {e}"); conn.rollback() if conn else None
        finally:
            if cur: cur.close();
            if conn: conn.close()
    return redirect(redirect_url)

# ... (and so on for all your other notes/folder routes)

# --- Oracle Chat (Gemini) Routes ---
@app.route('/oracle_chat')
@login_required
def oracle_chat_page():
    log_activity('view_oracle_chat_page')
    if not ORACLE_API_ENDPOINT_URL:
        flash("Oracle Chat is currently unavailable (API endpoint not configured). Please check server logs.", "error")
    return render_template('oracle_chat.html')

# MODIFIED: The function signature is updated to accept the request context.
def run_oracle_query_in_background(job_id, payload, ip_address, user_agent, path):
    """
    This function runs in a separate thread.
    It makes the slow API call and updates the job status dictionary.
    """
    try:
        headers = { "Content-Type": "application/json" }
        if ORACLE_API_FUNCTION_KEY:
            headers["X-Api-Key"] = ORACLE_API_FUNCTION_KEY

        # MODIFIED: Increased timeout from 90 to 180 seconds (3 minutes)
        response = requests.post(ORACLE_API_ENDPOINT_URL, json=payload, headers=headers, timeout=180)
        response.raise_for_status()
        
        response_data = response.json()
        llm_reply = response_data.get("reply")
        
        if llm_reply is None:
            raise ValueError("Received an empty or invalid reply from the Oracle API.")
            
        oracle_jobs[job_id] = {"status": "complete", "reply": llm_reply}
        log_activity(
            'oracle_response_received_from_external_api',
            details={'job_id': job_id, 'response_start': llm_reply[:100]},
            ip_address=ip_address, user_agent=user_agent, path=path
        )

    except Exception as e:
        print(f"[ERROR] Background thread error for job {job_id}: {e}")
        traceback.print_exc()
        oracle_jobs[job_id] = {"status": "error", "reply": f"An error occurred: {str(e)}"}
        # MODIFIED: Pass the captured context to log_activity. This now works correctly.
        log_activity(
            'oracle_api_error',
            details={'job_id': job_id, 'error': str(e)},
            ip_address=ip_address, user_agent=user_agent, path=path
        )

@app.route('/api/oracle_chat_start', methods=['POST'])
@login_required
def api_oracle_chat_start():
    if not ORACLE_API_ENDPOINT_URL:
        return jsonify({"error": "Oracle Chat API endpoint is not configured."}), 503

    client_data = request.get_json()
    if not client_data or 'message' not in client_data:
        return jsonify({"error": "Invalid request payload."}), 400

    job_id = str(uuid.uuid4())
    oracle_jobs[job_id] = {"status": "pending", "reply": None}
    
    payload = {
        "message": client_data.get('message'),
        "history": client_data.get('history', [])
    }
    
    # NEW: Capture request context before starting the thread.
    ip_address = request.remote_addr
    user_agent = request.headers.get('User-Agent')
    path = request.path

    # MODIFIED: Pass the captured context as arguments to the thread's target function.
    thread = threading.Thread(
        target=run_oracle_query_in_background,
        args=(job_id, payload, ip_address, user_agent, path)
    )
    thread.daemon = True
    thread.start()
    
    log_activity('oracle_query_sent_to_external_api', details={'job_id': job_id, 'prompt_start': payload['message'][:100]})

    return jsonify({"job_id": job_id}), 202

@app.route('/api/oracle_chat_status/<job_id>', methods=['GET'])
@login_required
def api_oracle_chat_status(job_id):
    job = oracle_jobs.get(job_id)
    if not job:
        return jsonify({"status": "error", "reply": "Job not found."}), 404
        
    if job['status'] == 'complete' or job['status'] == 'error':
        return jsonify(oracle_jobs.pop(job_id))
    else:
        return jsonify(job)


# --- Other Routes (Omitted for brevity, assumed to be unchanged) ---
# ... (Your existing code for db_test, view_activity_log, etc. goes here) ...
@app.route('/db_test')
@login_required
def db_test():
    # This function remains unchanged.
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT version();")
        db_version = cur.fetchone()
        return f"Database connection successful!<br/>PostgreSQL version: {db_version[0]}"
    except Exception as e:
        return f"Database connection failed: {e}", 500
    finally:
        if conn:
            if cur: cur.close()
            conn.close()

# ... (rest of your file) ...

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5167)), debug=True)