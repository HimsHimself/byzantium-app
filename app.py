import os
import psycopg2
import json
import requests
from psycopg2.extras import Json, RealDictCursor
from flask import Flask, request, session, redirect, url_for, render_template, flash, jsonify
from functools import wraps
import traceback
import datetime

app = Flask(__name__)
# --- Main Configuration (Secret Keys, Passwords, API URLs) ---
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-for-local-testing")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "dev-password")
ORACLE_API_ENDPOINT_URL = os.environ.get("ORACLE_API_ENDPOINT_URL")
ORACLE_API_FUNCTION_KEY = os.environ.get("ORACLE_API_FUNCTION_KEY")


# --- Database & Logging Helpers ---
def get_db_connection():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise Exception("DATABASE_URL is not set")
    return psycopg2.connect(db_url)

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

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# --- Main & Auth Routes ---
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

@app.route('/')
@login_required
def hello():
    return render_template('index.html')


# --- Notes and Folders Routes ---
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
            flash(f"Folder '{folder_name}' created.", 'success')
            redirect_url = url_for('notes_page', folder_id=new_folder_id)
        except Exception as e:
            flash(f"Error: {e}", 'error'); conn.rollback() if conn else None
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
        cur.execute("SELECT * FROM folders WHERE id = %s AND user_id = 1", (folder_id_for_sidebar_list,) if folder_id_for_sidebar_list else (None,))
        current_folder_context = cur.fetchone()
        parent_id = current_folder_context['parent_folder_id'] if current_folder_context else None
        cur.execute("SELECT * FROM folders WHERE parent_folder_id = %s AND user_id = 1 ORDER BY name", (parent_id,)) if parent_id else cur.execute("SELECT * FROM folders WHERE parent_folder_id IS NULL AND user_id = 1 ORDER BY name")
        folders_to_display_in_sidebar = cur.fetchall()
        notes_in_same_folder = []
        if current_note['folder_id']:
            cur.execute("SELECT id, title, updated_at FROM notes WHERE folder_id = %s AND user_id = 1 ORDER BY updated_at DESC", (current_note['folder_id'],))
        notes_in_same_folder = cur.fetchall()
        return render_template('notes.html', folders=folders_to_display_in_sidebar, current_folder=current_folder_context, notes_in_folder=notes_in_same_folder, current_note=current_note)
    except Exception as e:
        traceback.print_exc(); flash("Error viewing note.", "error")
        return redirect(url_for('notes_page'))
    finally:
        if cur: cur.close();
        if conn: conn.close()
        
@app.route('/notes/folder/<int:folder_id>/add_note', methods=['POST'])
@login_required
def add_note(folder_id):
    note_title = request.form.get('note_title','').strip()
    if not note_title: flash('Note title cannot be empty.', 'error'); return redirect(url_for('notes_page', folder_id=folder_id))
    conn = None; cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO notes (title, content, folder_id, user_id, created_at, updated_at) VALUES (%s, %s, %s, 1, NOW(), NOW()) RETURNING id",(note_title, '', folder_id))
        new_note_id = cur.fetchone()[0]
        conn.commit()
        flash(f"Note '{note_title}' created.", 'success')
        return redirect(url_for('view_note', note_id=new_note_id))
    except Exception as e:
        flash(f"Error: {e}", 'error'); conn.rollback() if conn else None
    finally:
        if cur: cur.close();
        if conn: conn.close()
    return redirect(url_for('notes_page', folder_id=folder_id))

@app.route('/note/<int:note_id>/update', methods=['POST'])
@login_required
def update_note(note_id):
    new_content = request.form.get('note_content', '')
    conn = None; cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE notes SET content = %s, updated_at = NOW() WHERE id = %s AND user_id = 1", (new_content, note_id))
        conn.commit()
        flash('Note updated.', 'success')
    except Exception as e:
        flash(f"Error updating note: {e}", 'error'); conn.rollback() if conn else None
    finally:
        if cur: cur.close();
        if conn: conn.close()
    return redirect(url_for('view_note', note_id=note_id))


# --- Oracle Chat (Gemini) Routes ---
@app.route('/oracle_chat')
@login_required
def oracle_chat_page():
    """
    This page now starts a NEW chat session each time it's visited.
    It creates a session in the DB and passes the new session_id to the template.
    """
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO chat_sessions (user_id, title) VALUES (1, %s) RETURNING id", (f"Session from {datetime.now().strftime('%Y-%m-%d %H:%M')}",))
        session_id = cur.fetchone()[0]
        conn.commit()
        log_activity('start_chat_session', details={'session_id': session_id})
        return render_template('oracle_chat.html', session_id=session_id)
    except Exception as e:
        print(f"Error starting chat session: {e}")
        traceback.print_exc()
        flash("Could not start a new Oracle Chat session. Please try again.", "error")
        return redirect(url_for('hello'))
    finally:
        if cur: cur.close()
        if conn: conn.close()


@app.route('/api/oracle_chat_query', methods=['POST'])
@login_required
def api_oracle_chat_query():
    """
    Handles a single chat message. Fetches history from DB, calls API, stores results.
    """
    if not ORACLE_API_ENDPOINT_URL:
        return jsonify({"error": "Oracle Chat API endpoint is not configured."}), 503

    data = request.get_json()
    if not data or 'message' not in data or 'session_id' not in data:
        return jsonify({"error": "Request must include 'message' and 'session_id'."}), 400

    user_message = data['message']
    session_id = data['session_id']
    conn = None
    cur = None

    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("INSERT INTO chat_messages (session_id, role, content) VALUES (%s, %s, %s)", (session_id, 'user', user_message))
        
        cur.execute("SELECT role, content AS text FROM chat_messages WHERE session_id = %s ORDER BY created_at ASC", (session_id,))
        db_history = cur.fetchall()
        history_for_api = [{'role': row['role'], 'parts': [{'text': row['text']}]} for row in db_history]

        payload = {
            "message": user_message, 
            "history": history_for_api[:-1]
        }
        headers = {"Content-Type": "application/json"}
        if ORACLE_API_FUNCTION_KEY:
            headers["X-Api-Key"] = ORACLE_API_FUNCTION_KEY

        response = requests.post(ORACLE_API_ENDPOINT_URL, json=payload, headers=headers, timeout=310)
        response.raise_for_status()
        response_data = response.json()
        llm_reply = response_data.get("reply")

        if not llm_reply:
            raise Exception("Received an empty reply from the Oracle API.")

        cur.execute("INSERT INTO chat_messages (session_id, role, content) VALUES (%s, %s, %s)", (session_id, 'model', llm_reply))
        
        conn.commit()
        return jsonify({"reply": llm_reply})

    except Exception as e:
        if conn: conn.rollback()
        print(f"Error in oracle chat query for session {session_id}: {e}")
        traceback.print_exc()
        error_detail = str(e)
        if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
             try: error_detail = e.response.json().get('error', e.response.text)
             except json.JSONDecodeError: error_detail = e.response.text
        return jsonify({"error": f"An error occurred: {error_detail}"}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=True)
