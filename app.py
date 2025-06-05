import os
import psycopg2
import json
from psycopg2.extras import Json, RealDictCursor # RealDictCursor to get dicts from DB
from flask import Flask, request, session, redirect, url_for, render_template, flash # Added flash
from functools import wraps
from datetime import datetime # For updated_at

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
        cur.close()
    except Exception as e:
        print(f"Error logging activity '{activity_type}': {e}")
    finally:
        if conn:
            conn.close()

# --- Authentication Decorator ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            # No request context here if used outside of a request, but it's fine for @app.route
            if request:
                 log_activity('unauthorized_access_attempt', details={"target_url": request.url})
            return redirect(url_for('login', next=request.url if request else url_for('hello')))
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
            session.permanent = True # Keep session for a reasonable time
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
    if request.endpoint and request.endpoint not in ['login', 'static', 'logout']:
        if 'logged_in' in session:
             log_activity('pageview')

@app.route('/')
@login_required
def hello():
    return render_template('index.html')

# --- Notes and Folders Routes ---
@app.route('/notes/')
@app.route('/notes/folder/<int:folder_id>')
@login_required
def notes_page(folder_id=None):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor) # Fetch rows as dictionaries

    current_folder = None
    folders_to_display = []
    notes_in_current_folder = []

    if folder_id:
        # Fetch current folder details
        cur.execute("SELECT * FROM folders WHERE id = %s AND user_id = 1", (folder_id,))
        current_folder = cur.fetchone()
        if not current_folder:
            flash('Folder not found.', 'error')
            return redirect(url_for('notes_page'))
        
        # Fetch subfolders of the current folder
        cur.execute("SELECT * FROM folders WHERE parent_folder_id = %s AND user_id = 1 ORDER BY name", (folder_id,))
        folders_to_display = cur.fetchall()
        
        # Fetch notes in the current folder
        cur.execute("SELECT * FROM notes WHERE folder_id = %s AND user_id = 1 ORDER BY updated_at DESC", (folder_id,))
        notes_in_current_folder = cur.fetchall()
    else:
        # Fetch root folders
        cur.execute("SELECT * FROM folders WHERE parent_folder_id IS NULL AND user_id = 1 ORDER BY name")
        folders_to_display = cur.fetchall()
        # No specific folder selected, so notes_in_current_folder remains empty or shows notes without a folder
        # For simplicity, let's keep it empty if no folder_id is provided
        # cur.execute("SELECT * FROM notes WHERE folder_id IS NULL AND user_id = 1 ORDER BY updated_at DESC")
        # notes_in_current_folder = cur.fetchall()


    cur.close()
    conn.close()
    log_activity('view_notes_page', details={'folder_id': folder_id if folder_id else "root"})
    return render_template('notes.html', 
                           folders=folders_to_display, 
                           notes_in_folder=notes_in_current_folder, 
                           current_folder=current_folder)

@app.route('/add_folder', methods=['POST'])
@login_required
def add_folder():
    folder_name = request.form.get('folder_name')
    parent_folder_id_str = request.form.get('parent_folder_id')
    parent_folder_id = int(parent_folder_id_str) if parent_folder_id_str else None

    if not folder_name:
        flash('Folder name cannot be empty.', 'error')
    else:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO folders (name, parent_folder_id, user_id, updated_at) VALUES (%s, %s, 1, NOW()) RETURNING id",
                (folder_name, parent_folder_id)
            )
            new_folder_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
            conn.close()
            log_activity('folder_created', details={'folder_name': folder_name, 'parent_id': parent_folder_id, 'new_folder_id': new_folder_id})
            flash(f"Folder '{folder_name}' created successfully.", 'success')
        except Exception as e:
            log_activity('folder_create_error', details={'folder_name': folder_name, 'error': str(e)})
            flash(f"Error creating folder: {e}", 'error')
            print(f"Error creating folder: {e}") # Also print to server log

    if parent_folder_id:
        return redirect(url_for('notes_page', folder_id=parent_folder_id))
    return redirect(url_for('notes_page'))


@app.route('/notes/folder/<int:folder_id>/add_note', methods=['POST'])
@login_required
def add_note(folder_id):
    note_title = request.form.get('note_title')
    # content = request.form.get('note_content', '') # Assuming content added later or through a different form

    if not note_title:
        flash('Note title cannot be empty.', 'error')
    else:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            # Create note with empty content initially
            cur.execute(
                "INSERT INTO notes (title, content, folder_id, user_id, updated_at) VALUES (%s, %s, %s, 1, NOW()) RETURNING id",
                (note_title, '', folder_id) # Content is empty string initially
            )
            new_note_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
            conn.close()
            log_activity('note_created', details={'note_title': note_title, 'folder_id': folder_id, 'note_id': new_note_id})
            flash(f"Note '{note_title}' created successfully.", 'success')
            return redirect(url_for('view_note', note_id=new_note_id)) # Redirect to view/edit the new note
        except Exception as e:
            log_activity('note_create_error', details={'note_title': note_title, 'folder_id': folder_id, 'error': str(e)})
            flash(f"Error creating note: {e}", 'error')
            print(f"Error creating note: {e}")

    return redirect(url_for('notes_page', folder_id=folder_id))

@app.route('/note/<int:note_id>', methods=['GET'])
@login_required
def view_note(note_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Fetch the specific note
    cur.execute("SELECT * FROM notes WHERE id = %s AND user_id = 1", (note_id,))
    current_note = cur.fetchone()

    if not current_note:
        flash('Note not found.', 'error')
        return redirect(url_for('notes_page'))

    # Fetch folders for the sidebar (context for breadcrumbs or navigation)
    # This logic might need refinement based on how deep nesting is displayed
    parent_folder_id_for_sidebar = current_note['folder_id']
    folders_to_display = []
    current_folder_for_sidebar = None

    if parent_folder_id_for_sidebar:
        cur.execute("SELECT * FROM folders WHERE id = %s AND user_id = 1", (parent_folder_id_for_sidebar,))
        current_folder_for_sidebar = cur.fetchone()
        cur.execute("SELECT * FROM folders WHERE parent_folder_id = %s AND user_id = 1 ORDER BY name", (parent_folder_id_for_sidebar,))
        folders_to_display = cur.fetchall()
    else: # Root notes or notes directly under root
        cur.execute("SELECT * FROM folders WHERE parent_folder_id IS NULL AND user_id = 1 ORDER BY name")
        folders_to_display = cur.fetchall()
    
    # Fetch notes in the same folder for context if needed, or simplify if note view is focused
    notes_in_same_folder = []
    if current_note['folder_id']:
        cur.execute("SELECT id, title, updated_at FROM notes WHERE folder_id = %s AND user_id = 1 AND id != %s ORDER BY updated_at DESC", (current_note['folder_id'], note_id))
        notes_in_same_folder = cur.fetchall()

    cur.close()
    conn.close()
    log_activity('view_note', details={'note_id': note_id, 'note_title': current_note['title']})
    return render_template('notes.html', 
                           folders=folders_to_display, 
                           current_folder=current_folder_for_sidebar, 
                           notes_in_folder=notes_in_same_folder, # This lists other notes in the folder
                           current_note=current_note)


@app.route('/note/<int:note_id>/update', methods=['POST'])
@login_required
def update_note(note_id):
    new_content = request.form.get('note_content')
    # Could also update title if a field is provided
    # new_title = request.form.get('note_title') 
    
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Fetch current title for logging
        cur.execute("SELECT title, folder_id FROM notes WHERE id = %s AND user_id = 1", (note_id,))
        note_data = cur.fetchone()
        if not note_data:
            flash("Note not found or permission denied.", "error")
            return redirect(url_for('notes_page'))

        cur.execute(
            "UPDATE notes SET content = %s, updated_at = NOW() WHERE id = %s AND user_id = 1",
            (new_content, note_id)
        )
        conn.commit()
        cur.close()
        conn.close()
        log_activity('note_updated', details={'note_id': note_id, 'note_title': note_data['title']})
        flash('Note updated successfully.', 'success')
        # Redirect back to the folder containing the note
        return redirect(url_for('notes_page', folder_id=note_data['folder_id']) if note_data['folder_id'] else url_for('notes_page'))
    except Exception as e:
        log_activity('note_update_error', details={'note_id': note_id, 'error': str(e)})
        flash(f"Error updating note: {e}", 'error')
        print(f"Error updating note: {e}")
    return redirect(url_for('view_note', note_id=note_id)) # Redirect back to note view on error

@app.route('/note/<int:note_id>/delete', methods=['POST'])
@login_required
def delete_note(note_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Fetch note details for logging and redirect
        cur.execute("SELECT title, folder_id FROM notes WHERE id = %s AND user_id = 1", (note_id,))
        note_data = cur.fetchone()
        
        if not note_data:
            flash("Note not found or permission denied.", "error")
            return redirect(url_for('notes_page'))

        cur.execute("DELETE FROM notes WHERE id = %s AND user_id = 1", (note_id,))
        conn.commit()
        cur.close()
        conn.close()
        log_activity('note_deleted', details={'note_id': note_id, 'note_title': note_data['title']})
        flash(f"Note '{note_data['title']}' deleted successfully.", 'success')
        # Redirect to the folder the note was in, or root notes page
        return redirect(url_for('notes_page', folder_id=note_data['folder_id']) if note_data['folder_id'] else url_for('notes_page'))
    except Exception as e:
        log_activity('note_delete_error', details={'note_id': note_id, 'error': str(e)})
        flash(f"Error deleting note: {e}", 'error')
        print(f"Error deleting note: {e}")
    # Fallback redirect
    return redirect(url_for('notes_page'))


# --- Other Routes ---
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

@app.route('/admin/activity_log')
@login_required
def view_activity_log():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor) # Use RealDictCursor
        cur.execute("SELECT id, user_id, activity_type, ip_address, path, details, TO_CHAR(timestamp, 'YYYY-MM-DD HH24:MI:SS TZ') as formatted_timestamp FROM activity_log ORDER BY timestamp DESC LIMIT 100")
        activities = cur.fetchall() # Fetches list of dicts
        cur.close()
        
        dashboard_url = url_for('hello')
        # This part will be improved by rendering a proper template later
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
            colnames = activities[0].keys()
            for name in colnames:
                html_output += f"<th>{name.replace('_', ' ').title()}</th>"
            html_output += "</tr></thead><tbody>"

            for activity_dict in activities:
                html_output += "<tr>"
                for col_name in colnames:
                    value = activity_dict.get(col_name)
                    if col_name == 'details' and value is not None:
                        html_output += f"<td><pre class='details-json'>{json.dumps(value, indent=2)}</pre></td>"
                    else:
                        html_output += f"<td>{str(value) if value is not None else ''}</td>"
                html_output += "</tr>"
            html_output += "</tbody></table>"
        else:
            html_output += "</tr></thead><tbody><tr><td colspan='7'>No activities found.</td></tr></tbody></table>"
        html_output += "</body></html>"
        return html_output
    except Exception as e:
        log_activity('error', details={"function": "view_activity_log", "error": str(e)})
        return f"Error fetching activity log: {e}", 500
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=True)
