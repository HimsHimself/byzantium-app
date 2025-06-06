import os
import psycopg2
import json
import requests # For making HTTP requests to the Cloud Function
from psycopg2.extras import Json, RealDictCursor
from flask import Flask, request, session, redirect, url_for, render_template, flash, jsonify
from functools import wraps
from datetime import datetime
import traceback
# import google.generativeai as genai # No longer needed directly here

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise ValueError("No SECRET_KEY set for Flask application.")

APP_PASSWORD = os.environ.get("APP_PASSWORD")
if not APP_PASSWORD:
    raise ValueError("No APP_PASSWORD set for Flask application.")

# Environment variables for the external Oracle API (Google Cloud Function)
ORACLE_API_ENDPOINT_URL = os.environ.get("ORACLE_API_ENDPOINT_URL")
ORACLE_API_FUNCTION_KEY = os.environ.get("ORACLE_API_FUNCTION_KEY") # Optional: Key for your Cloud Function

if not ORACLE_API_ENDPOINT_URL:
    print("[WARNING] ORACLE_API_ENDPOINT_URL not set. Oracle Chat functionality will be significantly impaired or disabled.")
# else:
    # print(f"[INFO] Oracle API Endpoint URL: {ORACLE_API_ENDPOINT_URL}")


# --- Database Helper ---
def get_db_connection():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise Exception("DATABASE_URL is not set")
    return psycopg2.connect(db_url)

# --- Activity Logging Helper ---
def log_activity(activity_type, details=None):
    conn = None
    cur = None
    try:
        user_id = 1 if 'logged_in' in session else 0
        ip_address = request.remote_addr if request else 'N/A'
        user_agent = request.headers.get('User-Agent') if request else 'N/A'
        path = request.path if request else 'N/A'

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
    except Exception as e:
        print(f"Error logging activity '{activity_type}': {e}")
    finally:
        if conn:
            if cur: cur.close()
            conn.close()

# --- Authentication Decorator ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            target_url = request.url if request else url_for('hello')
            log_activity('unauthorized_access_attempt', details={"target_url": target_url})
            return redirect(url_for('login', next=target_url))
        return f(*args, **kwargs)
    return decorated_function

# --- Main Routes ---
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
       request.endpoint not in ['login', 'static', 'logout', 'api_oracle_chat_query']: # Exclude API endpoint
        log_activity('pageview')


@app.route('/')
@login_required
def hello():
    return render_template('index.html')

# --- Notes and Folders Routes ---
# (These routes remain unchanged from the previous version, full code omitted for brevity but assumed present)
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

@app.route('/folder/<int:folder_id>/rename', methods=['POST'])
@login_required
def rename_folder(folder_id):
    new_folder_name = request.form.get('new_folder_name','').strip()
    redirect_url = url_for('notes_page', folder_id=folder_id)
    conn = None; cur = None
    try:
        if not new_folder_name: flash('Folder name cannot be empty.', 'error')
        else:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT parent_folder_id FROM folders WHERE id = %s AND user_id = 1", (folder_id,))
            folder_data = cur.fetchone()
            if not folder_data: flash("Folder not found.", "error"); return redirect(url_for('notes_page'))
            cur.execute("UPDATE folders SET name = %s, updated_at = NOW() WHERE id = %s AND user_id = 1", (new_folder_name, folder_id))
            conn.commit()
            log_activity('folder_renamed', details={'folder_id': folder_id, 'new_name': new_folder_name})
            flash(f"Folder renamed to '{new_folder_name}'.", 'success')
            parent_id = folder_data['parent_folder_id']
            redirect_url = url_for('notes_page', folder_id=parent_id) if parent_id else url_for('notes_page')
    except Exception as e:
        log_activity('folder_rename_error', details={'folder_id': folder_id, 'error': str(e)})
        flash(f"Error: {e}", 'error'); print(f"Error: {e}"); conn.rollback() if conn else None
    finally:
        if cur: cur.close();
        if conn: conn.close()
    return redirect(redirect_url)

@app.route('/folder/<int:folder_id>/delete', methods=['POST'])
@login_required
def delete_folder(folder_id):
    conn = None; cur = None
    parent_id_of_deleted_folder = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT name, parent_folder_id FROM folders WHERE id = %s AND user_id = 1", (folder_id,))
        folder_data = cur.fetchone()
        if not folder_data: flash("Folder not found.", "error"); return redirect(url_for('notes_page'))
        folder_name_for_log = folder_data['name']
        parent_id_of_deleted_folder = folder_data['parent_folder_id']
        cur.execute("DELETE FROM folders WHERE id = %s AND user_id = 1", (folder_id,))
        conn.commit()
        log_activity('folder_deleted', details={'folder_id': folder_id, 'folder_name': folder_name_for_log})
        flash(f"Folder '{folder_name_for_log}' deleted.", 'success')
    except Exception as e:
        log_activity('folder_delete_error', details={'folder_id': folder_id, 'error': str(e)})
        flash(f"Error: {e}", 'error'); print(f"Error: {e}"); conn.rollback() if conn else None
    finally:
        if cur: cur.close();
        if conn: conn.close()
    if parent_id_of_deleted_folder: return redirect(url_for('notes_page', folder_id=parent_id_of_deleted_folder))
    return redirect(url_for('notes_page'))

@app.route('/notes/folder/<int:folder_id>/add_note', methods=['POST'])
@login_required
def add_note(folder_id):
    note_title = request.form.get('note_title','').strip()
    redirect_url = url_for('notes_page', folder_id=folder_id)
    if not note_title: flash('Note title cannot be empty.', 'error')
    else:
        conn = None; cur = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("INSERT INTO notes (title, content, folder_id, user_id, created_at, updated_at) VALUES (%s, %s, %s, 1, NOW(), NOW()) RETURNING id",(note_title, '', folder_id))
            new_note_id = cur.fetchone()[0]
            conn.commit()
            log_activity('note_created', details={'note_title': note_title, 'folder_id': folder_id, 'note_id': new_note_id})
            flash(f"Note '{note_title}' created.", 'success')
            redirect_url = url_for('view_note', note_id=new_note_id)
        except Exception as e:
            log_activity('note_create_error', details={'note_title': note_title, 'folder_id': folder_id, 'error': str(e)})
            flash(f"Error: {e}", 'error'); print(f"Error: {e}"); conn.rollback() if conn else None
        finally:
            if cur: cur.close();
            if conn: conn.close()
    return redirect(redirect_url)

@app.route('/note/<int:note_id>', methods=['GET'])
@login_required
def view_note(note_id):
    conn = None; cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM notes WHERE id = %s AND user_id = 1", (note_id,))
        current_note = cur.fetchone()
        if not current_note: flash('Note not found.', 'error'); return redirect(url_for('notes_page'))

        folder_id_for_sidebar_list = current_note['folder_id']
        current_folder_context = None
        folders_to_display_in_sidebar = []
        if folder_id_for_sidebar_list:
            cur.execute("SELECT * FROM folders WHERE id = %s AND user_id = 1", (folder_id_for_sidebar_list,))
            current_folder_context = cur.fetchone()
            parent_of_current_folder_id = current_folder_context['parent_folder_id'] if current_folder_context else None
            if parent_of_current_folder_id:
                 cur.execute("SELECT * FROM folders WHERE parent_folder_id = %s AND user_id = 1 ORDER BY name", (parent_of_current_folder_id,))
            else:
                 cur.execute("SELECT * FROM folders WHERE parent_folder_id IS NULL AND user_id = 1 ORDER BY name")
            folders_to_display_in_sidebar = cur.fetchall()
        else:
            cur.execute("SELECT * FROM folders WHERE parent_folder_id IS NULL AND user_id = 1 ORDER BY name")
            folders_to_display_in_sidebar = cur.fetchall()

        notes_in_same_folder = []
        if current_note['folder_id']:
            cur.execute("SELECT id, title, updated_at FROM notes WHERE folder_id = %s AND user_id = 1 AND id != %s ORDER BY updated_at DESC", (current_note['folder_id'], note_id))
        else:
            cur.execute("SELECT id, title, updated_at FROM notes WHERE folder_id IS NULL AND user_id = 1 AND id != %s ORDER BY updated_at DESC", (note_id,))
        notes_in_same_folder = cur.fetchall()

        log_activity('view_note_details', details={'note_id': note_id, 'note_title': current_note['title']})
        return render_template('notes.html', folders=folders_to_display_in_sidebar, current_folder=current_folder_context, notes_in_folder=notes_in_same_folder, current_note=current_note)
    except Exception as e:
        traceback.print_exc(); flash("Error viewing note.", "error"); log_activity('error', details={'function': 'view_note', 'error': str(e), 'note_id': note_id})
        return redirect(url_for('notes_page'))
    finally:
        if cur: cur.close();
        if conn: conn.close()

@app.route('/note/<int:note_id>/rename', methods=['POST'])
@login_required
def rename_note(note_id):
    new_note_title = request.form.get('new_note_title','').strip()
    redirect_url = url_for('view_note', note_id=note_id)
    if not new_note_title: flash("Note title cannot be empty.", "error")
    else:
        conn = None; cur = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT id FROM notes WHERE id = %s AND user_id = 1", (note_id,))
            if not cur.fetchone(): flash("Note not found.", "error"); return redirect(url_for('notes_page'))
            cur.execute("UPDATE notes SET title = %s, updated_at = NOW() WHERE id = %s AND user_id = 1",(new_note_title, note_id))
            conn.commit()
            log_activity('note_renamed', details={'note_id': note_id, 'new_title': new_note_title})
            flash('Note renamed.', 'success')
        except Exception as e:
            log_activity('note_rename_error', details={'note_id': note_id, 'error': str(e)})
            flash(f"Error: {e}", 'error'); print(f"Error: {e}"); conn.rollback() if conn else None
        finally:
            if cur: cur.close();
            if conn: conn.close()
    return redirect(redirect_url)

@app.route('/note/<int:note_id>/update', methods=['POST'])
@login_required
def update_note(note_id):
    new_content = request.form.get('note_content', '')
    redirect_url = url_for('view_note', note_id=note_id)
    conn = None; cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT title FROM notes WHERE id = %s AND user_id = 1", (note_id,))
        note_data = cur.fetchone()
        if not note_data: flash("Note not found.", "error"); return redirect(url_for('notes_page'))
        cur.execute("UPDATE notes SET content = %s, updated_at = NOW() WHERE id = %s AND user_id = 1", (new_content, note_id))
        conn.commit()
        log_activity('note_updated', details={'note_id': note_id, 'note_title': note_data['title']})
        flash('Note updated.', 'success')
    except Exception as e:
        log_activity('note_update_error', details={'note_id': note_id, 'error': str(e)})
        flash(f"Error: {e}", 'error'); print(f"Error: {e}"); conn.rollback() if conn else None
    finally:
        if cur: cur.close();
        if conn: conn.close()
    return redirect(redirect_url)

@app.route('/note/<int:note_id>/delete', methods=['POST'])
@login_required
def delete_note(note_id):
    conn = None; cur = None
    folder_note_was_in = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT title, folder_id FROM notes WHERE id = %s AND user_id = 1", (note_id,))
        note_data = cur.fetchone()
        if not note_data: flash("Note not found.", "error"); return redirect(url_for('notes_page'))
        folder_note_was_in = note_data['folder_id']
        cur.execute("DELETE FROM notes WHERE id = %s AND user_id = 1", (note_id,))
        conn.commit()
        log_activity('note_deleted', details={'note_id': note_id, 'note_title': note_data['title']})
        flash(f"Note '{note_data['title']}' deleted.", 'success')
    except Exception as e:
        log_activity('note_delete_error', details={'note_id': note_id, 'error': str(e)})
        flash(f"Error: {e}", 'error'); print(f"Error: {e}"); conn.rollback() if conn else None
    finally:
        if cur: cur.close();
        if conn: conn.close()
    if folder_note_was_in: return redirect(url_for('notes_page', folder_id=folder_note_was_in))
    return redirect(url_for('notes_page'))

# --- Oracle Chat (Gemini) Routes - Updated to call external API ---
@app.route('/oracle_chat')
@login_required
def oracle_chat_page():
    log_activity('view_oracle_chat_page')
    if not ORACLE_API_ENDPOINT_URL:
        flash("Oracle Chat is currently unavailable (API endpoint not configured). Please check server logs.", "error")
    return render_template('oracle_chat.html')

@app.route('/api/oracle_chat_query', methods=['POST'])
@login_required
def api_oracle_chat_query():
    if not ORACLE_API_ENDPOINT_URL:
        log_activity('oracle_api_error', details={'error': 'ORACLE_API_ENDPOINT_URL not configured'})
        return jsonify({"error": "Oracle Chat API endpoint is not configured on the server."}), 503

    try:
        client_data = request.get_json()
        if not client_data:
            return jsonify({"error": "Invalid JSON payload."}), 400

        user_message_text = client_data.get('message')
        client_history = client_data.get('history', [])

        if not user_message_text:
            return jsonify({"error": "No message provided."}), 400

        # Prepare payload for the external Google Cloud Function
        payload = {
            "message": user_message_text,
            "history": client_history
            # The Cloud Function will have its own SYSTEM_INSTRUCTION for Gemini
        }

        headers = {
            "Content-Type": "application/json"
        }
        # Add authentication header if your Cloud Function is secured with an API key
        if ORACLE_API_FUNCTION_KEY:
            headers["X-Api-Key"] = ORACLE_API_FUNCTION_KEY # Or whatever header your CF expects

        log_activity('oracle_query_sent_to_external_api', details={'prompt_start': user_message_text[:100], 'history_len': len(client_history)})

        # Call the external API (Google Cloud Function)
        response = requests.post(ORACLE_API_ENDPOINT_URL, json=payload, headers=headers, timeout=60) # Increased timeout

        # Check if the external API call was successful
        response.raise_for_status() # Raises an HTTPError for bad responses (4XX or 5XX)

        response_data = response.json()
        llm_reply = response_data.get("reply")

        if llm_reply is None:
            log_activity('oracle_external_api_empty_reply', details=response_data)
            return jsonify({"error": "Received an empty or invalid reply from the Oracle API."}), 500

        log_activity('oracle_response_received_from_external_api', details={'response_start': llm_reply[:100]})
        return jsonify({"reply": llm_reply})

    except requests.exceptions.Timeout:
        print(f"[ERROR] Timeout calling Oracle API: {ORACLE_API_ENDPOINT_URL}")
        traceback.print_exc()
        log_activity('oracle_api_error', details={'error': 'Timeout calling external Oracle API'})
        return jsonify({"error": "The Oracle is contemplating deeply and took too long to respond. Please try again."}), 504 # Gateway Timeout
    except requests.exceptions.HTTPError as http_err:
        error_message = f"Oracle API HTTP error: {http_err.response.status_code} - {http_err.response.text}"
        print(f"[ERROR] {error_message}")
        traceback.print_exc()
        log_activity('oracle_api_error', details={'error': str(http_err), 'status_code': http_err.response.status_code, 'response': http_err.response.text[:200]})
        # Try to return a more user-friendly error from the upstream service if possible
        try:
            upstream_error = http_err.response.json().get("error", "An error occurred with the Oracle's external service.")
            return jsonify({"error": upstream_error}), http_err.response.status_code
        except ValueError: # If upstream response is not JSON
             return jsonify({"error": f"An error occurred with the Oracle's external service (Status: {http_err.response.status_code})."}), http_err.response.status_code

    except Exception as e:
        print(f"[ERROR] Error in /api/oracle_chat_query (external call): {e}")
        traceback.print_exc()
        log_activity('oracle_api_error', details={'error': str(e)})
        return jsonify({"error": f"An unexpected error occurred while communicating with the Oracle: {str(e)}"}), 500


# --- Other Routes ---
@app.route('/db_test')
@login_required
def db_test():
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

@app.route('/admin/activity_log')
@login_required
def view_activity_log():
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, user_id, activity_type, ip_address, path, details, TO_CHAR(timestamp, 'YYYY-MM-DD HH24:MI:SS TZ') as formatted_timestamp FROM activity_log ORDER BY timestamp DESC LIMIT 100")
        activities = cur.fetchall()

        dashboard_url = url_for('hello')
        html_output = f"""
        <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Activity Log</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
        <style> body {{ font-family: 'Inter', sans-serif; background-color: #f8fafc; color: #334155; padding: 20px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 0.9em; }}
        th, td {{ border: 1px solid #cbd5e1; padding: 10px; text-align: left; word-break: break-word; }}
        th {{ background-color: #e2e8f0; color: #4B0082; }}
        tr:nth-child(even) {{ background-color: #f1f5f9; }}
        a {{ color: #4B0082; text-decoration: none; }} a:hover {{ text-decoration: underline; }}
        .details-json {{ max-width: 300px; max-height: 150px; overflow: auto; white-space: pre-wrap; background-color: #eef2ff; padding: 5px; border-radius: 4px; font-family: monospace; font-size: 0.8em;}}
        </style></head><body>
        <h1 class="text-2xl font-semibold mb-4" style="color: #4B0082;">Activity Log <span style="color: #DAA520;">&dagger;</span> (Last 100)</h1>
        <p><a href="{dashboard_url}">Back to Dashboard</a></p>
        <table><thead><tr>
        """
        if activities:
            colnames = list(activities[0].keys())
            for name in colnames:
                html_output += f"<th>{name.replace('_', ' ').title()}</th>"
            html_output += "</tr></thead><tbody>"

            for activity_dict in activities:
                html_output += "<tr>"
                for col_name in colnames:
                    value = activity_dict.get(col_name)
                    if col_name == 'details' and value is not None:
                        try:
                            details_json = json.dumps(value, indent=2)
                        except TypeError:
                            details_json = str(value) # Fallback if not JSON serializable
                        html_output += f"<td><pre class='details-json'>{details_json}</pre></td>"
                    else:
                        html_output += f"<td>{str(value) if value is not None else ''}</td>"
                html_output += "</tr>"
            html_output += "</tbody></table>"
        else:
            default_colnames = ['ID', 'User ID', 'Activity Type', 'IP Address', 'Path', 'Details', 'Formatted Timestamp']
            html_output += f"</tr></thead><tbody><tr><td colspan='{len(default_colnames)}'>No activities found.</td></tr></tbody></table>" # Corrected colspan
        html_output += "</body></html>"
        return html_output
    except Exception as e:
        log_activity('error', details={"function": "view_activity_log", "error": str(e)})
        return f"Error fetching activity log: {e}", 500
    finally:
        if conn:
            if cur: cur.close()
            conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5167)), debug=True)
