import os
import psycopg2
import json
import requests
import uuid
import threading
import pytz
import re
from psycopg2.extras import Json, RealDictCursor
from flask import Flask, request, session, redirect, url_for, render_template, flash, jsonify, g
from functools import wraps
from datetime import datetime, timedelta
import traceback
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
from google.cloud import storage
from werkzeug.utils import secure_filename
import io
import base64

app = Flask(__name__)

# --- Configuration and Secrets ---
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise ValueError("No SECRET_KEY set for Flask application.")

APP_PASSWORD = os.environ.get("APP_PASSWORD")
if not APP_PASSWORD:
    raise ValueError("No APP_PASSWORD set for Flask application.")

ORACLE_API_ENDPOINT_URL = os.environ.get("ORACLE_API_ENDPOINT_URL")
ORACLE_API_FUNCTION_KEY = os.environ.get("ORACLE_API_FUNCTION_KEY")
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

if not ORACLE_API_ENDPOINT_URL:
    print("[WARNING] ORACLE_API_ENDPOINT_URL not set. Oracle Chat functionality will be significantly impaired or disabled.")
if not GCS_BUCKET_NAME or not GOOGLE_CREDENTIALS_JSON:
    print("[WARNING] GCS environment variables not set. Image uploads for the collection will not work.")

# --- In-memory store for background job status ---
oracle_jobs = {}

# --- Database Helper ---
def get_db():
    if 'db' not in g:
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            raise Exception("DATABASE_URL is not set")
        g.db = psycopg2.connect(db_url)
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

# --- Activity Logging Helper ---
def log_activity(activity_type, details=None, ip_address=None, user_agent=None, path=None):
    try:
        user_id = 0
        try:
            if 'logged_in' in session:
                user_id = 1
        except RuntimeError:
            user_id = -1 # System/background tasks

        final_ip_address = ip_address or (request.remote_addr if request else 'N/A')
        final_user_agent = user_agent or (request.headers.get('User-Agent') if request else 'N/A')
        final_path = path or (request.path if request else 'N/A')

        if details is not None and not isinstance(details, dict):
            details = {"info": str(details)}

        sql = """
            INSERT INTO activity_log (user_id, activity_type, ip_address, user_agent, path, details)
            VALUES (%s, %s, %s, %s, %s, %s);
        """
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, activity_type, final_ip_address, final_user_agent, final_path, Json(details) if details else None))
        conn.commit()
    except Exception as e:
        print(f"--- CRITICAL: Error logging activity '{activity_type}': {e} ---")
        traceback.print_exc()

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

# --- SNQL & EditorJS Helper Functions ---
REFERENCE_PATTERN = re.compile(r'\[\[(.*?)\]\]')

def process_editorjs_content_for_snql(cursor, note_id, editor_data):
    """
    Processes EditorJS JSON data to extract [[link]] references,
    updates note_references table, and saves the content.
    """
    conn = cursor.connection
    referenced_titles = set()

    # 1. Extract all [[Title]] references from the EditorJS JSON blocks
    if isinstance(editor_data, dict) and 'blocks' in editor_data:
        for block in editor_data['blocks']:
            if block.get('type') in ['paragraph', 'header'] and 'text' in block.get('data', {}):
                found = REFERENCE_PATTERN.findall(block['data']['text'])
                for title in found:
                    if title.strip():
                        referenced_titles.add(title.strip())

    target_note_ids = set()
    if referenced_titles:
        cursor.execute("SELECT id, title FROM notes WHERE title = ANY(%s)", (list(referenced_titles),))
        found_notes = {note['title']: note['id'] for note in cursor.fetchall()}

        for title in referenced_titles:
            if title in found_notes:
                target_note_ids.add(found_notes[title])
            else:
                # Create a new orphaned note for the link
                empty_content = {"time": datetime.now().timestamp(), "blocks": [], "version": "2.22.2"}
                cursor.execute("INSERT INTO notes (title, content, folder_id, user_id, created_at, updated_at) VALUES (%s, %s, %s, 1, NOW(), NOW()) RETURNING id", (title, Json(empty_content), None))
                new_note_id = cursor.fetchone()[0]
                target_note_ids.add(new_note_id)
                log_activity('note_created_from_link', details={'note_title': title, 'new_note_id': new_note_id, 'source_note_id': note_id})
    
    # 2. Update the note's content with the raw EditorJS JSON
    cursor.execute("UPDATE notes SET content = %s, updated_at = NOW() WHERE id = %s", (Json(editor_data), note_id))

    # 3. Rebuild the references in the note_references table
    cursor.execute("DELETE FROM note_references WHERE source_note_id = %s", (note_id,))
    if target_note_ids:
        args_list = [(note_id, target_id) for target_id in target_note_ids]
        cursor.executemany("INSERT INTO note_references (source_note_id, target_note_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", args_list)
    
    conn.commit()

def get_folder_tree(cursor):
    """
    Fetches all folders and organizes them into a nested tree structure.
    """
    cursor.execute("SELECT id, name, parent_folder_id FROM folders WHERE user_id = 1 ORDER BY name")
    all_folders = cursor.fetchall()
    
    folder_map = {f['id']: {**f, 'children': []} for f in all_folders}
    tree = []
    
    for folder_id, folder_data in folder_map.items():
        parent_id = folder_data['parent_folder_id']
        if parent_id and parent_id in folder_map:
            folder_map[parent_id]['children'].append(folder_data)
        elif not parent_id:
            tree.append(folder_data)
            
    return tree

def get_breadcrumbs(cursor, folder_id):
    """
    Generates a breadcrumb trail for a given folder ID.
    """
    if not folder_id:
        return []
    
    breadcrumbs = []
    current_id = folder_id
    while current_id:
        cursor.execute("SELECT id, name, parent_folder_id FROM folders WHERE id = %s", (current_id,))
        folder = cursor.fetchone()
        if folder:
            breadcrumbs.append({'id': folder['id'], 'name': folder['name']})
            current_id = folder['parent_folder_id']
        else:
            current_id = None
    return list(reversed(breadcrumbs))

# --- GCS Helpers ---
def upload_to_gcs(file_to_upload, bucket_name):
    """Uploads a file to a given GCS bucket and returns its filename."""
    if not file_to_upload or not file_to_upload.filename:
        return None
    try:
        credentials_info = json.loads(os.environ.get('GOOGLE_CREDENTIALS_JSON'))
        storage_client = storage.Client.from_service_account_info(credentials_info)
    except Exception as e:
        print(f"Error creating GCS client: {e}")
        return None
    bucket = storage_client.bucket(bucket_name)
    original_filename = secure_filename(file_to_upload.filename)
    filename_ext = os.path.splitext(original_filename)[1]
    unique_filename = f"{uuid.uuid4().hex}{filename_ext}"
    blob = bucket.blob(unique_filename)
    try:
        blob.upload_from_file(file_to_upload, content_type=file_to_upload.content_type)
        return unique_filename 
    except Exception as e:
        print(f"Error uploading to GCS: {e}")
        traceback.print_exc()
        return None

def delete_from_gcs(blob_name, bucket_name):
    """Deletes a file from a given GCS bucket based on its blob name."""
    if not blob_name or not GCS_BUCKET_NAME:
        return
    try:
        credentials_json_str = os.environ.get('GOOGLE_CREDENTIALS_JSON')
        if not credentials_json_str:
            print("ERROR: GOOGLE_CREDENTIALS_JSON environment variable not set.")
            return
        credentials_info = json.loads(credentials_json_str)
        storage_client = storage.Client.from_service_account_info(credentials_info)
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        if blob.exists():
            blob.delete()
            log_activity('gcs_file_deleted', details={'blob_name': blob_name})
        else:
            print(f"Blob '{blob_name}' not found for deletion.")
    except Exception as e:
        print(f"Error deleting from GCS: {e}")
        log_activity('gcs_delete_error', details={'blob_name': blob_name, 'error': str(e)})

# --- Main Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == APP_PASSWORD:
            session['logged_in'] = True
            session.permanent = True
            log_activity('login_success')
            return redirect(request.args.get('next') or url_for('hello'))
        else:
            log_activity('login_failure', details={"reason": "Invalid password"})
            flash('Invalid Password. Please try again.', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    if 'logged_in' in session:
        log_activity('logout')
        session.pop('logged_in', None)
        flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.before_request
def before_request_handler():
    if 'logged_in' in session and request.endpoint and request.endpoint not in ['login', 'static', 'logout', 'api_oracle_chat_start', 'api_oracle_chat_status', 'api_notes_search']:
        log_activity('pageview')

@app.route('/')
@login_required
def hello():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) as total_items, SUM(approximate_value) as total_value FROM antiques WHERE user_id = 1")
            collection_stats = cur.fetchone()
            cur.execute("SELECT id, title, updated_at FROM notes WHERE user_id = 1 ORDER BY updated_at DESC LIMIT 4")
            recent_notes = cur.fetchall()
            london_tz = pytz.timezone("Europe/London")
            today_london = datetime.now(london_tz).date()
            cur.execute("SELECT SUM(calories) as total FROM food_log WHERE user_id = 1 AND DATE(log_time AT TIME ZONE 'Europe/London') = %s;", (today_london,))
            calories_today_result = cur.fetchone()
            calories_today = calories_today_result['total'] if calories_today_result and calories_today_result['total'] is not None else 0
            cur.execute("SELECT activity_type, timestamp FROM activity_log WHERE user_id = 1 ORDER BY id DESC LIMIT 5")
            recent_activities = cur.fetchall()
        context = {"stats": collection_stats, "recent_notes": recent_notes, "calories_today": calories_today, "recent_activities": recent_activities}
        return render_template('index.html', **context)
    except Exception as e:
        log_activity('error', details={"function": "hello_dashboard", "error": str(e)})
        flash("Could not load dashboard data.", "error")
        return render_template('index.html')

# --- Notes and Folders Routes ---
@app.route('/notes/')
@app.route('/notes/folder/<int:folder_id>')
@login_required
def notes_page(folder_id=None):
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            current_folder, notes_in_current_folder = None, []
            folder_tree = get_folder_tree(cur)
            breadcrumbs = get_breadcrumbs(cur, folder_id)
            if folder_id:
                cur.execute("SELECT * FROM folders WHERE id = %s AND user_id = 1", (folder_id,))
                current_folder = cur.fetchone()
                if not current_folder:
                    flash('Folder not found.', 'error')
                    return redirect(url_for('notes_page'))
                cur.execute("SELECT id, title, updated_at FROM notes WHERE folder_id = %s AND user_id = 1 ORDER BY updated_at DESC", (folder_id,))
                notes_in_current_folder = cur.fetchall()
            cur.execute("SELECT id, title, updated_at FROM notes WHERE folder_id IS NULL AND user_id = 1 ORDER BY updated_at DESC")
            orphaned_notes = cur.fetchall()
            return render_template('notes.html', 
                                   folder_tree=folder_tree, 
                                   breadcrumbs=breadcrumbs, 
                                   notes_in_folder=notes_in_current_folder, 
                                   current_folder=current_folder, 
                                   current_note=None, 
                                   orphaned_notes=orphaned_notes,
                                   content_as_json_string=None,
                                   backlinks=[],
                                   outgoing_links=[])
    except Exception as e:
        traceback.print_exc()
        flash("Error loading notes.", "error")
        return redirect(url_for('hello'))

@app.route('/note/<int:note_id>', methods=['GET'])
@login_required
def view_note(note_id):
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM notes WHERE id = %s AND user_id = 1", (note_id,))
            current_note = cur.fetchone()
            if not current_note:
                flash('Note not found.', 'error')
                return redirect(url_for('notes_page'))
            
            # The content is JSON, so we pass it as a JSON string for the editor
            content_as_json_string = json.dumps(current_note.get('content') or {"blocks": []})
            
            cur.execute("SELECT n.id, n.title FROM notes n JOIN note_references nr ON n.id = nr.source_note_id WHERE nr.target_note_id = %s ORDER BY n.title;", (note_id,))
            backlinks = cur.fetchall()
            cur.execute("SELECT n.id, n.title FROM notes n JOIN note_references nr ON n.id = nr.target_note_id WHERE nr.source_note_id = %s ORDER BY n.title;", (note_id,))
            outgoing_links = cur.fetchall()
            
            folder_id = current_note['folder_id']
            current_folder = None
            if folder_id:
                cur.execute("SELECT * FROM folders WHERE id = %s AND user_id = 1", (folder_id,))
                current_folder = cur.fetchone()
            
            folder_tree = get_folder_tree(cur)
            breadcrumbs = get_breadcrumbs(cur, folder_id)
            notes_in_same_folder = []
            if folder_id:
                cur.execute("SELECT id, title, updated_at FROM notes WHERE folder_id = %s AND user_id = 1 ORDER BY updated_at DESC", (folder_id,))
                notes_in_same_folder = cur.fetchall()
            
            cur.execute("SELECT id, title, updated_at FROM notes WHERE folder_id IS NULL AND user_id = 1 ORDER BY updated_at DESC")
            orphaned_notes = cur.fetchall()

        return render_template('notes.html', 
                               folder_tree=folder_tree, 
                               breadcrumbs=breadcrumbs, 
                               current_folder=current_folder, 
                               notes_in_folder=notes_in_same_folder, 
                               current_note=current_note, 
                               orphaned_notes=orphaned_notes,
                               content_as_json_string=content_as_json_string,
                               backlinks=backlinks, 
                               outgoing_links=outgoing_links)
    except Exception as e:
        traceback.print_exc()
        flash(f"Error viewing note: {e}", "error")
        log_activity('view_note_error', details={'note_id': note_id, 'error': str(e)})
        return redirect(url_for('notes_page'))

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
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO folders (name, parent_folder_id, user_id, created_at, updated_at) VALUES (%s, %s, 1, NOW(), NOW()) RETURNING id", (folder_name, parent_folder_id))
                new_folder_id = cur.fetchone()[0]
            conn.commit()
            log_activity('folder_created', details={'folder_name': folder_name, 'parent_id': parent_folder_id, 'new_folder_id': new_folder_id})
            flash(f"Folder '{folder_name}' created.", 'success')
        except Exception as e:
            conn.rollback()
            log_activity('folder_create_error', details={'folder_name': folder_name, 'error': str(e)})
            flash(f"Error: {e}", 'error')
    return redirect(redirect_url)

@app.route('/notes/folder/<int:folder_id>/add_note', methods=['POST'])
@login_required
def add_note(folder_id):
    note_title = request.form.get('note_title','').strip()
    redirect_url = url_for('notes_page', folder_id=folder_id)
    if not note_title:
        flash('Note title cannot be empty.', 'error')
    else:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM notes WHERE title = %s AND user_id = 1", (note_title,))
                if cur.fetchone():
                    flash(f"A note with the title '{note_title}' already exists.", 'error')
                    return redirect(redirect_url)
                
                # New notes have a default empty EditorJS structure
                empty_content = {"time": datetime.now().timestamp(), "blocks": [], "version": "2.22.2"}
                cur.execute("INSERT INTO notes (title, content, folder_id, user_id, created_at, updated_at) VALUES (%s, %s, %s, 1, NOW(), NOW()) RETURNING id", (note_title, Json(empty_content), folder_id))
                new_note_id = cur.fetchone()[0]
            conn.commit()
            log_activity('note_created', details={'note_title': note_title, 'folder_id': folder_id, 'note_id': new_note_id})
            flash(f"Note '{note_title}' created.", 'success')
            redirect_url = url_for('view_note', note_id=new_note_id)
        except Exception as e:
            conn.rollback()
            log_activity('note_create_error', details={'note_title': note_title, 'folder_id': folder_id, 'error': str(e)})
            flash(f"Error: {e}", 'error')
    return redirect(redirect_url)

@app.route('/note/<int:note_id>/update', methods=['POST'])
@login_required
def update_note(note_id):
    editor_data = request.get_json()
    if not editor_data:
        return jsonify({"status": "error", "message": "Invalid data."}), 400
    
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT title FROM notes WHERE id = %s AND user_id = 1", (note_id,))
            note_data = cur.fetchone()
            if not note_data:
                return jsonify({"status": "error", "message": "Note not found."}), 404
            
            process_editorjs_content_for_snql(cur, note_id, editor_data)
        
        log_activity('note_updated', details={'note_id': note_id, 'note_title': note_data['title']})
        return jsonify({"status": "success", "message": "Note updated."})
    except Exception as e:
        conn.rollback()
        log_activity('note_update_error', details={'note_id': note_id, 'error': str(e)})
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"An unexpected error occurred: {e}"}), 500

@app.route('/note/<int:note_id>/delete', methods=['POST'])
@login_required
def delete_note(note_id):
    conn = get_db()
    folder_note_was_in = None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT title, folder_id FROM notes WHERE id = %s AND user_id = 1", (note_id,))
            note_data = cur.fetchone()
            if not note_data: 
                flash("Note not found.", "error")
                return redirect(url_for('notes_page'))
            folder_note_was_in = note_data['folder_id']
            cur.execute("DELETE FROM notes WHERE id = %s AND user_id = 1", (note_id,))
        conn.commit()
        log_activity('note_deleted', details={'note_id': note_id, 'note_title': note_data['title']})
        flash(f"Note '{note_data['title']}' deleted.", 'success')
    except Exception as e:
        conn.rollback()
        log_activity('note_delete_error', details={'note_id': note_id, 'error': str(e)})
        flash(f"Error: {e}", 'error')
    if folder_note_was_in: 
        return redirect(url_for('notes_page', folder_id=folder_note_was_in))
    return redirect(url_for('notes_page'))

@app.route('/folder/<int:folder_id>/delete', methods=['POST'])
@login_required
def delete_folder(folder_id):
    conn = get_db()
    parent_id_of_deleted_folder = None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name, parent_folder_id FROM folders WHERE id = %s AND user_id = 1", (folder_id,))
            folder_data = cur.fetchone()
            if not folder_data: 
                flash("Folder not found.", "error")
                return redirect(url_for('notes_page'))
            folder_name_for_log = folder_data['name']
            parent_id_of_deleted_folder = folder_data['parent_folder_id']
            cur.execute("DELETE FROM folders WHERE id = %s AND user_id = 1", (folder_id,))
        conn.commit()
        log_activity('folder_deleted', details={'folder_id': folder_id, 'folder_name': folder_name_for_log})
        flash(f"Folder '{folder_name_for_log}' deleted.", 'success')
    except Exception as e:
        conn.rollback()
        log_activity('folder_delete_error', details={'folder_id': folder_id, 'error': str(e)})
        flash(f"Error: {e}", 'error')
        
    if parent_id_of_deleted_folder: 
        return redirect(url_for('notes_page', folder_id=parent_id_of_deleted_folder))
    return redirect(url_for('notes_page'))

# --- API and Other Routes (largely unchanged) ---

@app.route('/api/notes/search')
@login_required
def api_notes_search():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    conn = get_db()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT title FROM notes WHERE title ILIKE %s AND user_id = 1 LIMIT 10", (f"{query}%",))
        results = cur.fetchall()
    return jsonify([row['title'] for row in results])

# ... (The rest of the file for Food Log, Collection, Oracle Chat, etc. remains the same) ...
# --- Food Log Routes ---
@app.route('/food_log', methods=['GET', 'POST'])
@login_required
def food_log_page():
    if request.method == 'POST':
        log_type = request.form.get('log_type')
        description = request.form.get('description', '').strip()
        calories_str = request.form.get('calories')
        log_time_str = request.form.get('log_time')
        errors = []
        if not log_type: errors.append("Please select a log type.")
        if not description: errors.append("Description cannot be empty.")
        if not log_time_str: errors.append("Please provide a date and time.")
        calories = int(calories_str) if calories_str and calories_str.isdigit() else None
        try:
            log_time_dt = datetime.fromisoformat(log_time_str)
        except (ValueError, TypeError):
            errors.append("Invalid date and time format.")
            log_time_dt = None
        if errors:
            for error in errors: flash(error, 'error')
        else:
            try:
                conn = get_db()
                with conn.cursor() as cur:
                    sql = "INSERT INTO food_log (log_type, description, calories, log_time, user_id, created_at) VALUES (%s, %s, %s, %s, 1, NOW())"
                    cur.execute(sql, (log_type, description, calories, log_time_dt))
                conn.commit()
                flash('Food log saved successfully!', 'success')
                log_activity('food_logged', details={'log_type': log_type, 'description': description, 'calories': calories})
                return redirect(url_for('food_log_page'))
            except Exception as e:
                conn.rollback()
                log_activity('food_log_error', details={'error': str(e)})
                flash(f"Error saving to database: {e}", 'error')
    london_tz = pytz.timezone("Europe/London")
    now_in_london = datetime.now(london_tz)
    default_datetime = now_in_london.strftime('%Y-%m-%dT%H:%M')
    return render_template('food_log.html', default_datetime=default_datetime)

@app.route('/food_log/view')
@login_required
def view_food_log():
    try:
        conn = get_db()
        thirty_days_ago = datetime.now() - timedelta(days=30)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM food_log WHERE user_id = 1 AND log_time >= %s ORDER BY log_time DESC", (thirty_days_ago,))
            logs = cur.fetchall()
            for log in logs:
                if log.get('log_time'): log['log_date'] = log['log_time'].date()
            today_london = datetime.now(pytz.timezone("Europe/London")).date()
            sql_today_calories = "SELECT SUM(calories) as total FROM food_log WHERE user_id = 1 AND DATE(log_time AT TIME ZONE 'Europe/London') = %s;"
            cur.execute(sql_today_calories, (today_london,))
            result = cur.fetchone()
            today_total_calories = int(result['total']) if result and result['total'] is not None else 0
        chart_url = None
        if logs:
            df = pd.DataFrame(logs)
            df['date'] = pd.to_datetime(df['log_time']).dt.date
            daily_calories = df.groupby('date')['calories'].sum().reset_index()
            today = datetime.now().date()
            date_range = pd.date_range(start=today - timedelta(days=29), end=today, freq='D').to_frame(index=False, name='date')
            date_range['date'] = date_range['date'].dt.date
            daily_calories = pd.merge(date_range, daily_calories, on='date', how='left').fillna(0)
            daily_calories = daily_calories.sort_values(by='date', ascending=True)
            plt.style.use('seaborn-v0_8-whitegrid')
            fig, ax = plt.subplots(figsize=(12, 6))
            bar_color, line_color, bg_color, text_color, grid_color = '#4B0082', '#C59B08', '#FDFDF6', '#2d3748', '#e2e8f0'
            fig.patch.set_facecolor(bg_color)
            ax.set_facecolor(bg_color)
            ax.bar(daily_calories['date'], daily_calories['calories'], color=bar_color, width=0.6, label='Total Daily Calories')
            ax.axhline(y=2000, color=line_color, linestyle='--', linewidth=2, label='2000 Calorie Target')
            ax.set_title('Total Daily Calories for the Last 30 Days', fontsize=16, fontweight='bold', color=text_color, pad=20)
            ax.set_xlabel('Date', fontsize=12, color=text_color, labelpad=10)
            ax.set_ylabel('Total Calories', fontsize=12, color=text_color, labelpad=10)
            ax.tick_params(axis='x', colors=text_color, rotation=45)
            ax.tick_params(axis='y', colors=text_color)
            ax.grid(color=grid_color)
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
            ax.xaxis.set_major_locator(mdates.DayLocator(interval=3))
            plt.setp(ax.get_xticklabels(), ha="right")
            ax.spines[['top', 'right', 'left', 'bottom']].set_color(grid_color)
            ax.legend(frameon=False, loc='upper left', bbox_to_anchor=(0, 1.1))
            plt.tight_layout()
            plot_path = os.path.join('static', 'calories_chart.png')
            if not os.path.exists('static'): os.makedirs('static')
            plt.savefig(plot_path)
            plt.close(fig)
            chart_url = url_for('static', filename='calories_chart.png')
        return render_template('view_food_log.html', logs=logs, chart_url=chart_url, today_total=today_total_calories)
    except Exception as e:
        log_activity('error', details={"function": "view_food_log", "error": str(e)})
        flash("Error fetching food log history.", "error")
        return redirect(url_for('food_log_page'))

# --- Collection Log Routes ---
@app.route('/collection')
@login_required
def collection_page():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM antiques WHERE user_id = 1 ORDER BY created_at DESC")
            items = cur.fetchall()
        return render_template('collection.html', items=items)
    except Exception as e:
        log_activity('error', details={"function": "collection_page", "error": str(e)})
        flash("Error fetching collection.", "error")
        return redirect(url_for('hello'))

@app.route('/collection/dashboard')
@login_required
def collection_dashboard():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM antiques WHERE user_id = 1")
            items = cur.fetchall()
        if not items:
            flash("No items in collection to create a dashboard.", "info")
            return redirect(url_for('collection_page'))
        df = pd.DataFrame(items)
        df['approximate_value'] = pd.to_numeric(df['approximate_value'], errors='coerce').fillna(0)
        total_value = df['approximate_value'].sum()
        total_items = len(df)
        items_with_value = df[df['approximate_value'] > 0].shape[0]
        value_by_type = df.groupby('item_type')['approximate_value'].sum().nlargest(10).sort_values()
        value_by_period = df.groupby('period')['approximate_value'].sum().nlargest(10).sort_values()
        plt.style.use('seaborn-v0_8-whitegrid')
        bg_color, text_color, bar_color = '#FDFDF6', '#2d3748', '#4B0082'
        fig1, ax1 = plt.subplots(figsize=(10, 6))
        fig1.patch.set_facecolor(bg_color); ax1.set_facecolor(bg_color)
        value_by_type.plot(kind='barh', ax=ax1, color=bar_color)
        ax1.set_title('Top 10 Collection Value by Item Type', fontsize=16, color=text_color, pad=20)
        ax1.set_xlabel('Total Approximate Value (£)', color=text_color); ax1.set_ylabel('Item Type', color=text_color)
        ax1.tick_params(colors=text_color); ax1.spines[['top', 'right']].set_visible(False)
        plt.tight_layout(); img1 = io.BytesIO(); plt.savefig(img1, format='png', bbox_inches='tight'); img1.seek(0)
        plot_url1 = base64.b64encode(img1.getvalue()).decode(); plt.close(fig1)
        fig2, ax2 = plt.subplots(figsize=(10, 6))
        fig2.patch.set_facecolor(bg_color); ax2.set_facecolor(bg_color)
        value_by_period.plot(kind='barh', ax=ax2, color=bar_color)
        ax2.set_title('Top 10 Collection Value by Period', fontsize=16, color=text_color, pad=20)
        ax2.set_xlabel('Total Approximate Value (£)', color=text_color); ax2.set_ylabel('Period', color=text_color)
        ax2.tick_params(colors=text_color); ax2.spines[['top', 'right']].set_visible(False)
        plt.tight_layout(); img2 = io.BytesIO(); plt.savefig(img2, format='png', bbox_inches='tight'); img2.seek(0)
        plot_url2 = base64.b64encode(img2.getvalue()).decode(); plt.close(fig2)
        stats = {"total_value": total_value, "total_items": total_items, "items_with_value": items_with_value, "average_value": total_value / items_with_value if items_with_value > 0 else 0}
        return render_template('collection_dashboard.html', stats=stats, plot_url1=plot_url1, plot_url2=plot_url2)
    except Exception as e:
        log_activity('error', details={"function": "collection_dashboard", "error": str(e)})
        flash("Error creating collection dashboard.", "error")
        traceback.print_exc()
        return redirect(url_for('collection_page'))

@app.route('/collection/item/<int:item_id>')
@login_required
def view_collection_item(item_id):
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM antiques WHERE id = %s AND user_id = 1", (item_id,))
            item = cur.fetchone()
        if not item:
            flash('Collection item not found.', 'error')
            return redirect(url_for('collection_page'))
        return render_template('view_item.html', item=item)
    except Exception as e:
        log_activity('error', details={"function": "view_collection_item", "error": str(e)})
        flash("Error fetching item details.", "error")
        return redirect(url_for('collection_page'))

@app.route('/collection/add', methods=['GET', 'POST'])
@login_required
def add_collection_item():
    conn = get_db()
    if request.method == 'POST':
        try:
            name = request.form.get('name')
            if not name:
                flash('Item Name is a required field.', 'error')
                return redirect(url_for('add_collection_item'))
            item_type = request.form.get('item_type').strip() if request.form.get('item_type') else None
            period = request.form.get('period').strip() if request.form.get('period') else None
            description = request.form.get('description')
            provenance = request.form.get('provenance')
            value_str = request.form.get('approximate_value')
            is_sellable = 'is_sellable' in request.form 
            approximate_value = float(value_str) if value_str else None
            image_url = None
            image_file = request.files.get('image')
            if image_file and image_file.filename != '':
                if GCS_BUCKET_NAME:
                    image_url = upload_to_gcs(image_file, GCS_BUCKET_NAME)
                    if not image_url:
                        flash('Image upload failed. Please try again.', 'error')
                        return redirect(url_for('add_collection_item'))
                else: flash('Image upload is not configured on the server.', 'error')
            sql = "INSERT INTO antiques (name, item_type, period, description, provenance, approximate_value, is_sellable, image_url, user_id, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, NOW(), NOW())"
            with conn.cursor() as cur:
                cur.execute(sql, (name, item_type, period, description, provenance, approximate_value, is_sellable, image_url))
            conn.commit()
            flash(f"Item '{name}' added to your collection!", 'success')
            log_activity('collection_item_added', details={'name': name, 'image_url': image_url})
            return redirect(url_for('collection_page'))
        except Exception as e:
            conn.rollback()
            log_activity('error', details={"function": "add_collection_item", "error": str(e)})
            flash(f"An error occurred while saving the item: {e}", "error")
            traceback.print_exc()
            return redirect(url_for('add_collection_item'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT item_type FROM antiques WHERE item_type IS NOT NULL AND item_type != '' ORDER BY item_type")
            item_types = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT DISTINCT period FROM antiques WHERE period IS NOT NULL AND period != '' ORDER BY period")
            periods = [row[0] for row in cur.fetchall()]
    except Exception as e:
        log_activity('error', details={"function": "add_collection_item_get", "error": str(e)})
        flash("Error fetching suggestions.", "error")
        item_types, periods = [], []
    return render_template('add_item.html', item_types=item_types, periods=periods)

@app.route('/collection/item/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_collection_item(item_id):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM antiques WHERE id = %s AND user_id = 1", (item_id,))
            item = cur.fetchone()
        if not item:
            flash('Collection item not found.', 'error')
            return redirect(url_for('collection_page'))
        if request.method == 'POST':
            name = request.form.get('name')
            if not name:
                flash('Item Name is a required field.', 'error')
                return redirect(url_for('edit_collection_item', item_id=item_id))
            approximate_value = float(request.form.get('approximate_value')) if request.form.get('approximate_value') else None
            image_url = item['image_url']
            image_file = request.files.get('image')
            if image_file and image_file.filename != '':
                if GCS_BUCKET_NAME:
                    if item['image_url']: delete_from_gcs(item['image_url'], GCS_BUCKET_NAME)
                    new_image_url = upload_to_gcs(image_file, GCS_BUCKET_NAME)
                    if new_image_url: image_url = new_image_url
                    else:
                        flash('New image upload failed. Please try again.', 'error')
                        return redirect(url_for('edit_collection_item', item_id=item_id))
                else: flash('Image upload is not configured on the server.', 'error')
            sql = "UPDATE antiques SET name=%s, item_type=%s, period=%s, description=%s, provenance=%s, approximate_value=%s, is_sellable=%s, image_url=%s, updated_at=NOW() WHERE id=%s AND user_id=1"
            with conn.cursor() as cur:
                cur.execute(sql, (name, request.form.get('item_type').strip() if request.form.get('item_type') else None, request.form.get('period').strip() if request.form.get('period') else None, request.form.get('description'), request.form.get('provenance'), approximate_value, 'is_sellable' in request.form, image_url, item_id))
            conn.commit()
            flash(f"Item '{name}' has been updated!", 'success')
            log_activity('collection_item_updated', details={'item_id': item_id, 'name': name})
            return redirect(url_for('view_collection_item', item_id=item_id))
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT item_type FROM antiques WHERE item_type IS NOT NULL AND item_type != '' ORDER BY item_type")
            item_types = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT DISTINCT period FROM antiques WHERE period IS NOT NULL AND period != '' ORDER BY period")
            periods = [row[0] for row in cur.fetchall()]
        return render_template('edit_item.html', item=item, item_types=item_types, periods=periods)
    except Exception as e:
        conn.rollback()
        log_activity('error', details={"function": "edit_collection_item", "error": str(e)})
        flash(f"An error occurred: {e}", "error")
        return redirect(url_for('collection_page'))

@app.route('/collection/item/<int:item_id>/delete', methods=['POST'])
@login_required
def delete_collection_item(item_id):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name, image_url FROM antiques WHERE id = %s AND user_id = 1", (item_id,))
            item = cur.fetchone()
        if not item:
            flash("Item not found.", "error")
            return redirect(url_for('collection_page'))
        if item['image_url']: delete_from_gcs(item['image_url'], GCS_BUCKET_NAME)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM antiques WHERE id = %s AND user_id = 1", (item_id,))
        conn.commit()
        log_activity('collection_item_deleted', details={'item_id': item_id, 'name': item['name']})
        flash(f"Item '{item['name']}' has been deleted.", 'success')
    except Exception as e:
        conn.rollback()
        log_activity('error', details={"function": "delete_collection_item", "error": str(e)})
        flash(f"An error occurred while deleting the item: {e}", "error")
    return redirect(url_for('collection_page'))

# --- Oracle Chat (Gemini) Routes ---
@app.route('/oracle_chat')
@login_required
def oracle_chat_page():
    if not ORACLE_API_ENDPOINT_URL:
        flash("Oracle Chat is currently unavailable (API endpoint not configured). Please check server logs.", "error")
    return render_template('oracle_chat.html')

def run_oracle_query_in_background(job_id, payload, ip_address, user_agent, path):
    try:
        headers = { "Content-Type": "application/json" }
        if ORACLE_API_FUNCTION_KEY: headers["X-Api-Key"] = ORACLE_API_FUNCTION_KEY
        response = requests.post(ORACLE_API_ENDPOINT_URL, json=payload, headers=headers, timeout=300)
        response.raise_for_status()
        response_data = response.json()
        llm_reply = response_data.get("reply")
        if llm_reply is None: raise ValueError("Received an empty or invalid reply from the Oracle API.")
        oracle_jobs[job_id] = {"status": "complete", "reply": llm_reply}
        log_activity('oracle_response_received_from_external_api', details={'job_id': job_id, 'response_start': llm_reply[:100]}, ip_address=ip_address, user_agent=user_agent, path=path)
    except requests.exceptions.Timeout:
        error_message = "The Oracle took more than 5 minutes to respond. The request has been cancelled."
        oracle_jobs[job_id] = {"status": "error", "reply": error_message}
        log_activity('oracle_api_error', details={'job_id': job_id, 'error': 'Timeout after 300 seconds'}, ip_address=ip_address, user_agent=user_agent, path=path)
    except Exception as e:
        error_message = f"The Oracle could not respond due to an unexpected error. Details: {str(e)}"
        oracle_jobs[job_id] = {"status": "error", "reply": error_message}
        log_activity('oracle_api_error', details={'job_id': job_id, 'error': str(e)}, ip_address=ip_address, user_agent=user_agent, path=path)

@app.route('/api/oracle_chat_start', methods=['POST'])
@login_required
def api_oracle_chat_start():
    if not ORACLE_API_ENDPOINT_URL: return jsonify({"error": "Oracle Chat API endpoint is not configured."}), 503
    client_data = request.get_json()
    if not client_data or 'message' not in client_data: return jsonify({"error": "Invalid request payload."}), 400
    job_id = str(uuid.uuid4())
    oracle_jobs[job_id] = {"status": "pending", "reply": None}
    payload = { "message": client_data.get('message'), "history": client_data.get('history', []) }
    thread = threading.Thread(target=run_oracle_query_in_background, args=(job_id, payload, request.remote_addr, request.headers.get('User-Agent'), request.path))
    thread.daemon = True
    thread.start()
    log_activity('oracle_query_sent_to_external_api', details={'job_id': job_id, 'prompt_start': payload['message'][:100]})
    return jsonify({"job_id": job_id}), 202

@app.route('/api/oracle_chat_status/<job_id>', methods=['GET'])
@login_required
def api_oracle_chat_status(job_id):
    job = oracle_jobs.get(job_id)
    if not job: return jsonify({"status": "error", "reply": "Job not found."}), 404
    if job['status'] in ['complete', 'error']:
        return jsonify(oracle_jobs.pop(job_id))
    else:
        return jsonify(job)

@app.route('/images/<path:filename>')
@login_required
def serve_private_image(filename):
    if not GCS_BUCKET_NAME or not GOOGLE_CREDENTIALS_JSON: return "Image serving is not configured.", 500
    try:
        credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        storage_client = storage.Client.from_service_account_info(credentials_info)
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(filename)
        if not blob.exists(): return "Image not found.", 404
        signed_url = blob.generate_signed_url(version="v4", expiration=timedelta(minutes=15), method="GET")
        return redirect(signed_url)
    except Exception as e:
        log_activity('error', details={"function": "serve_private_image", "error": str(e)})
        traceback.print_exc()
        return "Error serving image.", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5167)), debug=False)