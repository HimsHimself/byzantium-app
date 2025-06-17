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
import matplotlib
matplotlib.use('Agg') # Use a non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
from google.cloud import storage
from werkzeug.utils import secure_filename
import io
import base64
from markdown_it import MarkdownIt

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
    print("[WARNING] GCS environment variables not set. Image uploads will not work.")

# --- In-memory store for background job status ---
oracle_jobs = {}

# --- Markdown Renderer ---
md = MarkdownIt()

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

# --- SNQL Helper Functions ---
REFERENCE_PATTERN = re.compile(r'\[\[(.*?)\]\]')
SNQL_REF_PATTERN = re.compile(r'snql-ref:([0-9a-fA-F\-]{36})')
SNQL_BROKEN_REF_PATTERN = re.compile(r'snql-ref-broken:(.*?)(?=\s|\[\[|$$)')

def convert_db_content_to_raw_for_editing(cursor, db_content):
    if not db_content:
        return ""
    
    guids_to_find = SNQL_REF_PATTERN.findall(db_content)
    guid_to_title = {}
    if guids_to_find:
        cursor.execute("SELECT guid, title FROM notes WHERE guid = ANY(%s::uuid[])", (list(set(guids_to_find)),))
        for row in cursor.fetchall():
            guid_to_title[str(row['guid'])] = row['title']

    def replace_guid(match):
        guid = match.group(1)
        title = guid_to_title.get(guid, 'Unknown Note')
        return f"[[{title}]]"
    
    raw_content = SNQL_REF_PATTERN.sub(replace_guid, db_content)
    raw_content = SNQL_BROKEN_REF_PATTERN.sub(lambda m: f"[[{m.group(1)}]]", raw_content)
    return raw_content

def process_and_update_note_content(cursor, note_id, content_json):
    conn = cursor.connection
    all_referenced_titles = set()
    REFERENCE_PATTERN = re.compile(r'\[\[(.*?)\]\]')

    # 1. Find all [[links]] in the content JSON
    if 'blocks' in content_json and isinstance(content_json['blocks'], list):
        for block in content_json['blocks']:
            if block and 'data' in block and 'text' in block['data'] and isinstance(block['data']['text'], str):
                found_titles = REFERENCE_PATTERN.findall(block['data']['text'])
                for title in found_titles:
                    all_referenced_titles.add(title.strip())

    target_note_ids = set()
    found_notes_map = {}
    if all_referenced_titles:
        cursor.execute("SELECT id, title FROM notes WHERE title = ANY(%s)", (list(all_referenced_titles),))
        found_notes = cursor.fetchall()
        for note in found_notes:
            found_notes_map[note['title']] = {'id': note['id']}
            target_note_ids.add(note['id'])

    # 2. Create notes for titles that don't exist yet
    missing_titles = all_referenced_titles - set(found_notes_map.keys())
    for title in missing_titles:
        # Create a default empty content structure for the new note
        empty_content = {"time": int(datetime.now().timestamp() * 1000), "blocks": [], "version": "2.28.0"}
        cursor.execute("INSERT INTO notes (title, content, folder_id, user_id, created_at, updated_at) VALUES (%s, %s, %s, 1, NOW(), NOW()) RETURNING id", (title, Json(empty_content), None))
        new_note_id = cursor.fetchone()['id']
        target_note_ids.add(new_note_id)
        found_notes_map[title] = {'id': new_note_id}
        log_activity('note_created_from_link', details={'note_title': title, 'new_note_id': new_note_id, 'source_note_id': note_id})

    # 3. Update the content JSON with proper HTML links
    if 'blocks' in content_json and isinstance(content_json['blocks'], list):
        for block in content_json['blocks']:
             if block and 'data' in block and 'text' in block['data'] and isinstance(block['data']['text'], str):
                def replace_link(match):
                    title = match.group(1).strip()
                    if title in found_notes_map:
                        note_info = found_notes_map[title]
                        # Create an HTML link that Editor.js can render
                        return f'<a href="{url_for("view_note", note_id=note_info["id"])}">{title}</a>'
                    return match.group(0) 
                
                block['data']['text'] = REFERENCE_PATTERN.sub(replace_link, block['data']['text'])

    # 4. Save the processed JSON to the database
    cursor.execute("UPDATE notes SET content = %s, updated_at = NOW() WHERE id = %s", (Json(content_json), note_id))

    # 5. Update the note_references table for backlinks
    cursor.execute("DELETE FROM note_references WHERE source_note_id = %s", (note_id,))
    if target_note_ids:
        args_list = [(note_id, target_id) for target_id in target_note_ids]
        cursor.executemany("INSERT INTO note_references (source_note_id, target_note_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", args_list)
    
    # The final commit will be handled by the calling function `update_note`


# --- Helper for building the notes and folders tree ---
def get_full_notes_hierarchy(cursor):
    cursor.execute("SELECT id, name, parent_folder_id FROM folders WHERE user_id = 1 ORDER BY name")
    all_folders = cursor.fetchall()
    
    cursor.execute("SELECT id, title, folder_id FROM notes WHERE user_id = 1 ORDER BY title")
    all_notes = cursor.fetchall()

    folder_map = {f['id']: f for f in all_folders}
    
    for folder_id in folder_map:
        folder_map[folder_id]['children'] = []
        folder_map[folder_id]['notes'] = []

    for note in all_notes:
        folder_id = note['folder_id']
        if folder_id in folder_map:
            folder_map[folder_id]['notes'].append(note)

    tree = []
    for folder in all_folders:
        parent_id = folder['parent_folder_id']
        if parent_id in folder_map:
            folder_map[parent_id]['children'].append(folder)
        else: # Top-level folder
            tree.append(folder)
            
    orphaned_notes = [note for note in all_notes if not note['folder_id']]
    
    return tree, orphaned_notes

# --- GCS Helpers ---
def upload_to_gcs(file_to_upload, bucket_name):
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


# --- Markdown helper ---
def convert_markdown_to_editorjs_json(markdown_text):
    """
    A simple converter from Markdown text to Editor.js JSON structure.
    This helps migrate old notes to the new format.
    """
    if not markdown_text or not isinstance(markdown_text, str):
        return {"time": int(datetime.now().timestamp() * 1000), "blocks": [], "version": "2.28.0"}

    blocks = []
    # A simple regex to split by markdown headers
    # This will treat anything under a header as a single paragraph block
    parts = re.split(r'(^#+\s.*)', markdown_text, flags=re.MULTILINE)
    
    content_parts = [p.strip() for p in parts if p.strip()]

    for i, part in enumerate(content_parts):
        if part.startswith('#'):
            level = len(part.split(' ')[0])
            text = ' '.join(part.split(' ')[1:])
            blocks.append({"type": "header", "data": {"text": text, "level": level}})
            # Check if there is content following this header
            if i + 1 < len(content_parts) and not content_parts[i+1].startswith('#'):
                blocks.append({"type": "paragraph", "data": {"text": content_parts[i+1].replace('\n', ' ')}})

    if not blocks and markdown_text:
        blocks.append({"type": "paragraph", "data": {"text": markdown_text.replace('\n', ' ')}})

    return {
        "time": int(datetime.now().timestamp() * 1000),
        "blocks": blocks,
        "version": "2.28.0"
    }

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
       request.endpoint not in ['login', 'static', 'logout', 'api_oracle_chat_start', 'api_oracle_chat_status', 'api_notes_search', 'api_render_markdown', 'api_update_task_status']:
        log_activity('pageview')

@app.route('/')
@login_required
def hello():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1. Get Collection Statistics
            cur.execute("SELECT COUNT(*) as total_items, SUM(approximate_value) as total_value FROM antiques WHERE user_id = 1")
            collection_stats = cur.fetchone()

            # 2. Get Recently Updated Notes (Top 4)
            cur.execute("SELECT id, title, updated_at FROM notes WHERE user_id = 1 ORDER BY updated_at DESC LIMIT 4")
            recent_notes = cur.fetchall()
            
            # 3. Get Today's Calorie Count
            london_tz = pytz.timezone("Europe/London")
            today_london = datetime.now(london_tz).date()
            cur.execute("""
                SELECT SUM(calories) as total
                FROM food_log
                WHERE user_id = 1 AND DATE(log_time AT TIME ZONE 'Europe/London') = %s;
            """, (today_london,))
            calories_today_result = cur.fetchone()
            calories_today = calories_today_result['total'] if calories_today_result and calories_today_result['total'] is not None else 0

            # 4. Get Recent Activity (Top 5)
            cur.execute("SELECT activity_type, timestamp FROM activity_log WHERE user_id = 1 ORDER BY id DESC LIMIT 5")
            recent_activities = cur.fetchall()

            # 5. Get Incomplete tasks, ordered by due date
            cur.execute("""
                SELECT id, title, is_completed, due_date 
                FROM tasks 
                WHERE user_id = 1 AND is_completed = FALSE 
                ORDER BY due_date ASC NULLS FIRST, created_at ASC
            """)
            tasks = cur.fetchall()

        context = {
            "stats": collection_stats,
            "recent_notes": recent_notes,
            "calories_today": calories_today,
            "recent_activities": recent_activities,
            "tasks": tasks
        }
        return render_template('index.html', **context)
    except Exception as e:
        log_activity('error', details={"function": "hello_dashboard", "error": str(e)})
        traceback.print_exc()
        flash("Could not load dashboard data.", "error")
        # Render a failsafe static version if the DB query fails
        return render_template('index.html')

# --- Notes and Folders Routes ---
@app.route('/notes/')
@login_required
def notes_page():
    """Renders the main notes page without a specific note selected."""
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            notes_tree, orphaned_notes = get_full_notes_hierarchy(cur)

        return render_template('notes.html',
                               notes_tree=notes_tree,
                               orphaned_notes=orphaned_notes,
                               current_note=None)
    except Exception as e:
        traceback.print_exc()
        flash("Error loading notes page.", "error")
        return redirect(url_for('hello'))
    
@app.route('/note/<int:note_id>/move', methods=['POST'])
@login_required
def move_note(note_id):
    folder_id_str = request.form.get('folder_id')
    
    # Allow moving to root by setting folder_id to NULL
    folder_id = int(folder_id_str) if folder_id_str and folder_id_str.isdigit() else None
    
    conn = get_db()
    try:
        with conn.cursor() as cur:
            # Check if the note exists
            cur.execute("SELECT id FROM notes WHERE id = %s AND user_id = 1", (note_id,))
            if not cur.fetchone():
                flash('Note not found.', 'error')
                return redirect(url_for('notes_page'))
            
            # Update the folder_id for the note
            cur.execute("UPDATE notes SET folder_id = %s, updated_at = NOW() WHERE id = %s AND user_id = 1", (folder_id, note_id))
        conn.commit()
        flash('Note moved successfully.', 'success')
        log_activity('note_moved', details={'note_id': note_id, 'target_folder_id': folder_id})
    except Exception as e:
        conn.rollback()
        flash(f'Error moving note: {e}', 'error')
        log_activity('note_move_error', details={'note_id': note_id, 'error': str(e)})

    # Redirect back to the note that was just moved
    return redirect(url_for('view_note', note_id=note_id))

@app.route('/note/<int:note_id>', methods=['GET'])
@login_required
def view_note(note_id):
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            notes_tree, orphaned_notes = get_full_notes_hierarchy(cur)
            
            cur.execute("SELECT id, name FROM folders WHERE user_id = 1 ORDER BY name")
            all_folders_for_move = cur.fetchall()

            cur.execute("SELECT * FROM notes WHERE id = %s AND user_id = 1", (note_id,))
            current_note = cur.fetchone()
            if not current_note:
                flash('Note not found.', 'error')
                return redirect(url_for('notes_page'))

            # If content is old plain text, convert it to new Editor.js JSON format
            if isinstance(current_note['content'], str):
                current_note['content'] = convert_markdown_to_editorjs_json(current_note['content'])
            
            # The 'content_for_editing' is no longer needed as a separate variable
            # We will pass the whole 'current_note' object which now has JSON content

            cur.execute("SELECT n.id, n.title FROM notes n JOIN note_references nr ON n.id = nr.source_note_id WHERE nr.target_note_id = %s ORDER BY n.title;", (note_id,))
            backlinks = cur.fetchall()
            cur.execute("SELECT n.id, n.title FROM notes n JOIN note_references nr ON n.id = nr.target_note_id WHERE nr.source_note_id = %s ORDER BY n.title;", (note_id,))
            outgoing_links = cur.fetchall()

        return render_template('notes.html', 
                               notes_tree=notes_tree,
                               orphaned_notes=orphaned_notes,
                               current_note=current_note,
                               backlinks=backlinks,
                               outgoing_links=outgoing_links,
                               all_folders_for_move=all_folders_for_move)
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
    
    if not folder_name:
        flash('Folder name cannot be empty.', 'error')
    else:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO folders (name, parent_folder_id, user_id, created_at, updated_at) VALUES (%s, %s, 1, NOW(), NOW())", (folder_name, parent_folder_id))
            conn.commit()
            log_activity('folder_created', details={'folder_name': folder_name, 'parent_id': parent_folder_id})
            flash(f"Folder '{folder_name}' created.", 'success')
        except Exception as e:
            conn.rollback()
            log_activity('folder_create_error', details={'folder_name': folder_name, 'error': str(e)})
            flash(f"Error creating folder: {e}", 'error')
    
    return redirect(url_for('notes_page'))

@app.route('/folder/<int:folder_id>/delete', methods=['POST'])
@login_required
def delete_folder(folder_id):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM folders WHERE id = %s AND user_id = 1", (folder_id,))
            folder = cur.fetchone()
            if not folder: 
                flash("Folder not found.", "error")
            else:
                cur.execute("DELETE FROM folders WHERE id = %s AND user_id = 1", (folder_id,))
                conn.commit()
                log_activity('folder_deleted', details={'folder_id': folder_id, 'folder_name': folder['name']})
                flash(f"Folder '{folder['name']}' and all its contents have been deleted.", 'success')
    except Exception as e:
        conn.rollback()
        log_activity('folder_delete_error', details={'folder_id': folder_id, 'error': str(e)})
        flash(f"Error deleting folder: {e}", 'error')
        
    return redirect(url_for('notes_page'))

@app.route('/add_note', methods=['POST'])
@login_required
def add_note():
    note_title = request.form.get('note_title','').strip()
    folder_id_str = request.form.get('folder_id')
    folder_id = int(folder_id_str) if folder_id_str and folder_id_str.isdigit() else None

    if not note_title:
        flash('Note title cannot be empty.', 'error')
        return redirect(url_for('notes_page'))
        
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM notes WHERE title = %s AND user_id = 1", (note_title,))
            if cur.fetchone():
                flash(f"A note with the title '{note_title}' already exists.", 'error')
                return redirect(url_for('notes_page'))
            
            # Create a default content structure for the new note
            initial_content = {
                "time": int(datetime.now().timestamp() * 1000),
                "blocks": [
                    {"type": "header", "data": {"text": note_title, "level": 1}},
                    {"type": "paragraph", "data": {"text": "Start writing your new note here..."}}
                ],
                "version": "2.28.0"
            }
            
            cur.execute("INSERT INTO notes (title, content, folder_id, user_id, created_at, updated_at) VALUES (%s, %s, %s, 1, NOW(), NOW()) RETURNING id", (note_title, Json(initial_content), folder_id))
            new_note_id = cur.fetchone()[0]
        conn.commit()
        log_activity('note_created', details={'note_title': note_title, 'folder_id': folder_id, 'note_id': new_note_id})
        flash(f"Note '{note_title}' created.", 'success')
        return redirect(url_for('view_note', note_id=new_note_id))
    except Exception as e:
        conn.rollback()
        log_activity('note_create_error', details={'note_title': note_title, 'folder_id': folder_id, 'error': str(e)})
        flash(f"Error creating note: {e}", 'error')
        return redirect(url_for('notes_page'))


@app.route('/note/<int:note_id>/update', methods=['POST'])
@login_required
def update_note(note_id):
    # Ensure the request content type is JSON
    if not request.is_json:
        return jsonify({"success": False, "error": "Invalid content type, request must be JSON."}), 415

    data = request.get_json()
    note_content_json = data.get('content')
    note_title = data.get('title', '').strip()

    # Validate incoming data
    if not note_title:
        return jsonify({"success": False, "error": "Note title cannot be empty."}), 400
    if note_content_json is None:
        return jsonify({"success": False, "error": "Note content is missing from the request."}), 400

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Check if note exists
            cur.execute("SELECT id FROM notes WHERE id = %s AND user_id = 1", (note_id,))
            if not cur.fetchone():
                return jsonify({"success": False, "error": "Note not found."}), 404
            
            # Check if new title is unique (excluding the current note)
            cur.execute("SELECT id FROM notes WHERE title = %s AND user_id = 1 AND id != %s", (note_title, note_id))
            if cur.fetchone():
                return jsonify({"success": False, "error": f"Another note with the title '{note_title}' already exists."}), 400

            # Update the title and process the JSON content for links
            cur.execute("UPDATE notes SET title = %s, updated_at = NOW() WHERE id = %s", (note_title, note_id))
            process_and_update_note_content(cur, note_id, note_content_json)
        
        conn.commit()
        log_activity('note_updated', details={'note_id': note_id, 'note_title': note_title})
        return jsonify({"success": True, "message": "Note updated successfully."})
        
    except Exception as e:
        conn.rollback()
        log_activity('note_update_error', details={'note_id': note_id, 'error': str(e)})
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/note/<int:note_id>/delete', methods=['POST'])
@login_required
def api_delete_note(note_id):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT title FROM notes WHERE id = %s AND user_id = 1", (note_id,))
            note_data = cur.fetchone()
            if not note_data:
                return jsonify({"success": False, "error": "Note not found."}), 404

            cur.execute("DELETE FROM notes WHERE id = %s AND user_id = 1", (note_id,))
        conn.commit()
        log_activity('note_deleted', details={'note_id': note_id, 'note_title': note_data['title']})
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        log_activity('note_delete_error', details={'note_id': note_id, 'error': str(e)})
        return jsonify({"success": False, "error": str(e)}), 500

# --- Task Routes ---
@app.route('/api/tasks', methods=['POST'])
@login_required
def add_task():
    data = request.get_json()
    title = data.get('title', '').strip()
    due_date_str = data.get('due_date')

    if not title:
        return jsonify({'error': 'Title is required'}), 400

    due_date = None
    if due_date_str:
        try:
            due_date = datetime.fromisoformat(due_date_str)
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid due date format provided.'}), 400

    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO tasks (title, user_id, due_date, created_at, updated_at) VALUES (%s, 1, %s, NOW(), NOW()) RETURNING id, title, is_completed, due_date",
                (title, due_date)
            )
            new_task = cur.fetchone()
        conn.commit()
        log_activity('task_created', details={'title': title, 'due_date': due_date_str})
        return jsonify(new_task), 201
    except Exception as e:
        conn.rollback()
        log_activity('task_create_error', details={'error': str(e)})
        return jsonify({'error': str(e)}), 500
    
    
@app.route('/api/task/<int:task_id>/status', methods=['PUT'])
@login_required
def api_update_task_status(task_id):
    data = request.get_json()
    is_completed = data.get('is_completed')

    if is_completed is None:
        return jsonify({"error": "is_completed field is required"}), 400

    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("UPDATE tasks SET is_completed = %s, updated_at = NOW() WHERE id = %s AND user_id = 1", (is_completed, task_id))
            if cur.rowcount == 0:
                return jsonify({"error": "Task not found"}), 404
        conn.commit()
        log_activity('task_updated', details={'task_id': task_id, 'completed': is_completed})
        return jsonify({"success": True}), 200
    except Exception as e:
        conn.rollback()
        log_activity('task_update_error', details={'task_id': task_id, 'error': str(e)})
        return jsonify({"error": str(e)}), 500
        
# --- Log Routes ---
@app.route('/logs')
@login_required
def logs_page():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM logs WHERE user_id = 1 ORDER BY log_time DESC")
            logs = cur.fetchall()
            
            log_ids = [log['id'] for log in logs]
            attachments = {}
            if log_ids:
                cur.execute("SELECT log_id, file_name FROM log_attachments WHERE log_id = ANY(%s)", (log_ids,))
                for row in cur.fetchall():
                    if row['log_id'] not in attachments:
                        attachments[row['log_id']] = []
                    attachments[row['log_id']].append(row['file_name'])
            
            for log in logs:
                log['attachments'] = attachments.get(log['id'], [])

        return render_template('logs.html', logs=logs)
    except Exception as e:
        log_activity('error', details={'function': 'logs_page', 'error': str(e)})
        flash("Error fetching logs.", "error")
        traceback.print_exc()
        return redirect(url_for('hello'))

@app.route('/logs/add', methods=['GET', 'POST'])
@login_required
def add_log():
    if request.method == 'POST':
        try:
            log_type = request.form.get('log_type')
            title = request.form.get('title')
            content = request.form.get('content')
            log_time_str = request.form.get('log_time')
            log_time = datetime.fromisoformat(log_time_str) if log_time_str else datetime.now()

            structured_data = {}
            if log_type == 'workout':
                structured_data['duration_minutes'] = request.form.get('duration_minutes')
                structured_data['type'] = request.form.get('workout_type')
            elif log_type == 'reading':
                structured_data['book_title'] = request.form.get('book_title')
                structured_data['author'] = request.form.get('author')
                structured_data['pages_read'] = request.form.get('pages_read')
            elif log_type == 'gardening':
                structured_data['plants_tended'] = request.form.get('plants_tended')

            conn = get_db()
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO logs (log_type, title, content, structured_data, log_time, user_id, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, 1, NOW(), NOW()) RETURNING id
                    """,
                    (log_type, title, content, Json(structured_data) if structured_data else None, log_time)
                )
                log_id = cur.fetchone()['id']

                # Handle file upload for gardening
                if log_type == 'gardening' and 'photo' in request.files:
                    photo = request.files['photo']
                    if photo.filename != '':
                        file_name = upload_to_gcs(photo, GCS_BUCKET_NAME)
                        if file_name:
                            cur.execute(
                                """
                                INSERT INTO log_attachments (log_id, file_name, file_type, user_id, created_at)
                                VALUES (%s, %s, %s, 1, NOW())
                                """,
                                (log_id, file_name, photo.content_type)
                            )
                        else:
                            flash("Photo upload failed.", "error")

            conn.commit()
            flash(f"{log_type.capitalize()} log added successfully!", "success")
            log_activity(f'{log_type}_log_added', details={'title': title, 'log_id': log_id})
            return redirect(url_for('logs_page'))

        except Exception as e:
            conn.rollback()
            log_activity('log_add_error', details={'error': str(e)})
            flash(f"Error adding log: {e}", "error")
            traceback.print_exc()

    london_tz = pytz.timezone("Europe/London")
    now_in_london = datetime.now(london_tz)
    default_datetime = now_in_london.strftime('%Y-%m-%dT%H:%M')
    return render_template('add_log.html', default_datetime=default_datetime)


# --- API Routes ---
@app.route('/api/notes/search')
@login_required
def api_notes_search():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    
    conn = get_db()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT title FROM notes WHERE title ILIKE %s AND user_id = 1 LIMIT 10", (f"%{query}%",))
        results = cur.fetchall()
        
    return jsonify([row['title'] for row in results])

# --- Food Log Routes ---
@app.route('/food_log/add', methods=['GET', 'POST'])
@login_required
def add_food_log():
    if request.method == 'POST':
        log_type = request.form.get('log_type')
        description = request.form.get('description', '').strip()
        calories_str = request.form.get('calories')
        log_time_str = request.form.get('log_time')

        errors = []
        if not log_type:
            errors.append("Please select a log type.")
        if not description:
            errors.append("Description cannot be empty.")
        if not log_time_str:
            errors.append("Please provide a date and time.")

        calories = int(calories_str) if calories_str and calories_str.isdigit() else None

        try:
            log_time_dt = datetime.fromisoformat(log_time_str)
        except (ValueError, TypeError):
            errors.append("Invalid date and time format.")
            log_time_dt = None

        if errors:
            for error in errors:
                flash(error, 'error')
        else:
            try:
                conn = get_db()
                with conn.cursor() as cur:
                    sql = """
                        INSERT INTO food_log (log_type, description, calories, log_time, user_id, created_at)
                        VALUES (%s, %s, %s, %s, 1, NOW())
                    """
                    cur.execute(sql, (log_type, description, calories, log_time_dt))
                conn.commit()
                flash('Food log saved successfully!', 'success')
                log_activity('food_logged', details={
                    'log_type': log_type,
                    'description': description,
                    'calories': calories
                })
                return redirect(url_for('add_food_log'))
            except Exception as e:
                conn.rollback()
                log_activity('food_log_error', details={'error': str(e)})
                flash(f"Error saving to database: {e}", 'error')

    london_tz = pytz.timezone("Europe/London")
    now_in_london = datetime.now(london_tz)
    default_datetime = now_in_london.strftime('%Y-%m-%dT%H:%M')
    
    return render_template('add_food_log.html', default_datetime=default_datetime)



@app.route('/food_log/view')
@login_required
def view_food_log():
    try:
        conn = get_db()
        london_tz = pytz.timezone("Europe/London")
        today_london = datetime.now(london_tz).date()
        thirty_days_ago = datetime.now() - timedelta(days=30)
        
        today_total_calories = 0

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM food_log WHERE user_id = 1 AND log_time >= %s ORDER BY log_time DESC", (thirty_days_ago,))
            logs = cur.fetchall()

            for log in logs:
                if log.get('log_time'):
                    log['log_date'] = log['log_time'].date()

            sql_today_calories = """
                SELECT SUM(calories) as total
                FROM food_log
                WHERE user_id = 1 AND DATE(log_time AT TIME ZONE 'Europe/London') = %s;
            """
            cur.execute(sql_today_calories, (today_london,))
            result = cur.fetchone()
            if result and result['total'] is not None:
                today_total_calories = int(result['total'])
        
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
            ax.spines['top'].set_visible(False); ax.spines['right'].set_visible(False)
            ax.spines['left'].set_color(grid_color); ax.spines['bottom'].set_color(grid_color)
            ax.legend(frameon=False, loc='upper left', bbox_to_anchor=(0, 1.1))
            plt.tight_layout()
            
            img = io.BytesIO()
            plt.savefig(img, format='png')
            img.seek(0)
            chart_url = base64.b64encode(img.getvalue()).decode()
            plt.close(fig)

        return render_template('view_food_log.html', logs=logs, chart_url=chart_url, today_total=today_total_calories)

    except Exception as e:
        log_activity('error', details={"function": "view_food_log", "error": str(e)})
        flash("Error fetching food log history.", "error")
        traceback.print_exc()
        return redirect(url_for('add_food_log'))
    
@app.route('/food_log/edit/<int:log_id>', methods=['GET', 'POST'])
@login_required
def edit_food_log(log_id):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM food_log WHERE id = %s AND user_id = 1", (log_id,))
            log_entry = cur.fetchone()

        if not log_entry:
            flash('Food log entry not found.', 'error')
            return redirect(url_for('view_food_log'))

        if request.method == 'POST':
            log_type = request.form.get('log_type')
            description = request.form.get('description', '').strip()
            calories_str = request.form.get('calories')
            log_time_str = request.form.get('log_time')

            errors = []
            if not log_type:
                errors.append("Please select a log type.")
            if not description:
                errors.append("Description cannot be empty.")
            if not log_time_str:
                errors.append("Please provide a date and time.")

            calories = int(calories_str) if calories_str and calories_str.isdigit() else None

            try:
                log_time_dt = datetime.fromisoformat(log_time_str)
            except (ValueError, TypeError):
                errors.append("Invalid date and time format.")
                log_time_dt = None

            if errors:
                for error in errors:
                    flash(error, 'error')
                # Re-render the page with the submitted (but invalid) data
                log_entry['log_type'] = log_type
                log_entry['description'] = description
                log_entry['calories'] = calories
                log_entry['log_time'] = log_time_str # Keep the string for the input field
                return render_template('edit_food_log.html', log=log_entry)
            else:
                with conn.cursor() as cur:
                    sql = """
                        UPDATE food_log 
                        SET log_type = %s, description = %s, calories = %s, log_time = %s
                        WHERE id = %s AND user_id = 1
                    """
                    cur.execute(sql, (log_type, description, calories, log_time_dt, log_id))
                conn.commit()
                flash('Food log updated successfully!', 'success')
                log_activity('food_log_updated', details={'log_id': log_id, 'description': description})
                return redirect(url_for('view_food_log'))

        # GET request
        # Format the datetime object to the string required by the datetime-local input
        if isinstance(log_entry['log_time'], datetime):
             log_entry['log_time'] = log_entry['log_time'].strftime('%Y-%m-%dT%H:%M')
        
        return render_template('edit_food_log.html', log=log_entry)

    except Exception as e:
        conn.rollback()
        log_activity('error', details={"function": "edit_food_log", "log_id": log_id, "error": str(e)})
        flash(f"An error occurred: {e}", "error")
        return redirect(url_for('view_food_log'))

@app.route('/food_log/delete/<int:log_id>', methods=['POST'])
@login_required
def delete_food_log(log_id):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Optional: Fetch description for logging before deleting
            cur.execute("SELECT description FROM food_log WHERE id = %s AND user_id = 1", (log_id,))
            log_entry = cur.fetchone()
            if not log_entry:
                flash("Log entry not found.", "error")
                return redirect(url_for('view_food_log'))

            cur.execute("DELETE FROM food_log WHERE id = %s AND user_id = 1", (log_id,))
        conn.commit()
        flash('Food log entry deleted.', 'success')
        log_activity('food_log_deleted', details={'log_id': log_id, 'description': log_entry['description']})
    except Exception as e:
        conn.rollback()
        log_activity('error', details={"function": "delete_food_log", "log_id": log_id, "error": str(e)})
        flash(f"An error occurred: {e}", 'error')

    return redirect(url_for('view_food_log'))

# --- NEW: API Route for Calorie Estimation ---
@app.route('/api/food_log/estimate_calories', methods=['POST'])
@login_required
def api_estimate_calories():
    # 1. Check if the Oracle/Gemini API is configured
    if not ORACLE_API_ENDPOINT_URL:
        return jsonify({"error": "Calorie estimation service is not configured."}), 503

    # 2. Get the food description from the request
    data = request.get_json()
    description = data.get('description')
    if not description:
        return jsonify({"error": "Food description cannot be empty."}), 400

    # 3. Prepare the request to the external Gemini API
    # The prompt is specifically engineered to ask for a number only.
    prompt = f"Please provide a single numerical estimate for the calories in the following food item. Do not include any explanation, units like 'kcal', or commas. Just the number. Food: '{description}'"
    payload = {"message": prompt, "history": []}
    headers = {"Content-Type": "application/json"}
    if ORACLE_API_FUNCTION_KEY:
        headers["X-Api-Key"] = ORACLE_API_FUNCTION_KEY

    try:
        log_activity('calorie_estimation_sent', details={'description': description})
        
        # 4. Make the synchronous API call
        # A simple request is better here than the async job pattern used for the main chat.
        response = requests.post(ORACLE_API_ENDPOINT_URL, json=payload, headers=headers, timeout=20) # 20 second timeout
        response.raise_for_status()
        
        api_response = response.json()
        llm_reply = api_response.get("reply")

        if not llm_reply:
            raise ValueError("Received an empty reply from the calorie estimation API.")

        # 5. Extract the number from the model's response
        # Using regex to find the first sequence of digits in the reply.
        match = re.search(r'\d+', llm_reply)
        if match:
            estimated_calories = int(match.group(0))
            log_activity('calorie_estimation_success', details={'description': description, 'calories': estimated_calories})
            return jsonify({"calories": estimated_calories})
        else:
            raise ValueError(f"Could not extract a number from the API's response. Got: '{llm_reply}'")

    except requests.exceptions.Timeout:
        log_activity('calorie_estimation_error', details={'description': description, 'error': 'API Timeout'})
        return jsonify({"error": "The calorie estimation service timed out."}), 504
    except Exception as e:
        log_activity('calorie_estimation_error', details={'description': description, 'error': str(e)})
        traceback.print_exc()
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

# --- Collection Log Routes ---
@app.route('/collection')
@login_required
def collection_page():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query_search = request.args.get('q', '').strip()
            item_type_filter = request.args.get('item_type', '').strip()
            period_filter = request.args.get('period', '').strip()
            sellable_filter = request.args.get('is_sellable', '').strip()

            base_query = "SELECT * FROM antiques WHERE user_id = 1"
            where_clauses = []
            params = []
            
            if query_search:
                where_clauses.append("(name ILIKE %s OR description ILIKE %s OR item_type ILIKE %s OR period ILIKE %s OR provenance ILIKE %s)")
                search_term = f"%{query_search}%"
                params.extend([search_term] * 5)

            if item_type_filter:
                where_clauses.append("item_type = %s")
                params.append(item_type_filter)
            
            if period_filter:
                where_clauses.append("period = %s")
                params.append(period_filter)

            if sellable_filter == 'yes':
                where_clauses.append("is_sellable = TRUE")
            elif sellable_filter == 'no':
                where_clauses.append("is_sellable = FALSE")

            sql_query = f"{base_query} AND {' AND '.join(where_clauses)}" if where_clauses else base_query
            sql_query += " ORDER BY created_at DESC"

            cur.execute(sql_query, tuple(params))
            items = cur.fetchall()

            cur.execute("SELECT DISTINCT item_type FROM antiques WHERE user_id = 1 AND item_type IS NOT NULL AND item_type != '' ORDER BY item_type")
            item_types = [row['item_type'] for row in cur.fetchall()]
            
            cur.execute("SELECT DISTINCT period FROM antiques WHERE user_id = 1 AND period IS NOT NULL AND period != '' ORDER BY period")
            periods = [row['period'] for row in cur.fetchall()]

        current_filters = {
            'q': query_search,
            'item_type': item_type_filter,
            'period': period_filter,
            'is_sellable': sellable_filter
        }

        return render_template('collection.html', 
                               items=items, 
                               item_types=item_types, 
                               periods=periods,
                               filters=current_filters)
    except Exception as e:
        log_activity('error', details={"function": "collection_page", "error": str(e)})
        traceback.print_exc()
        flash("Error fetching collection.", "error")
        return redirect(url_for('hello'))



@app.route('/collection/dashboard')
@login_required
def collection_dashboard():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1. Get filter values from query parameters
            query_search = request.args.get('q', '').strip()
            item_type_filter = request.args.get('item_type', '').strip()
            period_filter = request.args.get('period', '').strip()
            sellable_filter = request.args.get('is_sellable', '').strip()

            # 2. Build a dynamic query based on filters
            base_query = "SELECT * FROM antiques WHERE user_id = 1"
            where_clauses = []
            params = []
            
            if query_search:
                where_clauses.append("(name ILIKE %s OR description ILIKE %s OR item_type ILIKE %s OR period ILIKE %s OR provenance ILIKE %s)")
                search_term = f"%{query_search}%"
                params.extend([search_term] * 5)

            if item_type_filter:
                where_clauses.append("item_type = %s")
                params.append(item_type_filter)
            
            if period_filter:
                where_clauses.append("period = %s")
                params.append(period_filter)

            if sellable_filter == 'yes':
                where_clauses.append("is_sellable = TRUE")
            elif sellable_filter == 'no':
                where_clauses.append("is_sellable = FALSE")

            if where_clauses:
                sql_query = f"{base_query} AND {' AND '.join(where_clauses)}"
            else:
                sql_query = base_query

            # 3. Execute the query to get the filtered items
            cur.execute(sql_query, tuple(params))
            items = cur.fetchall()

            # 4. Get distinct values for filter dropdowns (these should be from all items, not just filtered ones)
            cur.execute("SELECT DISTINCT item_type FROM antiques WHERE user_id = 1 AND item_type IS NOT NULL AND item_type != '' ORDER BY item_type")
            item_types = [row['item_type'] for row in cur.fetchall()]
            
            cur.execute("SELECT DISTINCT period FROM antiques WHERE user_id = 1 AND period IS NOT NULL AND period != '' ORDER BY period")
            periods = [row['period'] for row in cur.fetchall()]

        # Store current filters to pass back to the template
        current_filters = {
            'q': query_search,
            'item_type': item_type_filter,
            'period': period_filter,
            'is_sellable': sellable_filter
        }

        # If no items match the filters, render the dashboard with a message
        if not items:
            return render_template('collection_dashboard.html', 
                                   stats=None, 
                                   plot_url1=None, 
                                   plot_url2=None,
                                   filters=current_filters,
                                   item_types=item_types,
                                   periods=periods)

        # 5. If items are found, proceed with calculations and plotting
        df = pd.DataFrame(items)
        df['approximate_value'] = pd.to_numeric(df['approximate_value'], errors='coerce').fillna(0)

        # --- Key Stats ---
        total_value = df['approximate_value'].sum()
        total_items = len(df)
        items_with_value = df[df['approximate_value'] > 0].shape[0]
        
        stats = {
            "total_value": total_value,
            "total_items": total_items,
            "items_with_value": items_with_value,
            "average_value": total_value / items_with_value if items_with_value > 0 else 0
        }

        # --- Plotting ---
        plot_url1, plot_url2 = None, None
        plt.style.use('seaborn-v0_8-whitegrid')
        bg_color, text_color, bar_color = '#FDFDF6', '#2d3748', '#4B0082'

        # Plot 1: Value by Type
        if not df.empty and 'item_type' in df.columns and df['item_type'].notna().any():
            value_by_type = df.groupby('item_type')['approximate_value'].sum().nlargest(10).sort_values()
            if not value_by_type.empty:
                fig1, ax1 = plt.subplots(figsize=(10, 6))
                fig1.patch.set_facecolor(bg_color)
                ax1.set_facecolor(bg_color)
                value_by_type.plot(kind='barh', ax=ax1, color=bar_color)
                ax1.set_title('Top 10 Collection Value by Item Type', fontsize=16, color=text_color, pad=20)
                ax1.set_xlabel('Total Approximate Value (£)', color=text_color)
                ax1.set_ylabel('Item Type', color=text_color)
                ax1.tick_params(colors=text_color)
                ax1.spines['top'].set_visible(False); ax1.spines['right'].set_visible(False)
                plt.tight_layout()
                img1 = io.BytesIO()
                plt.savefig(img1, format='png', bbox_inches='tight')
                img1.seek(0)
                plot_url1 = base64.b64encode(img1.getvalue()).decode()
                plt.close(fig1)

        # Plot 2: Value by Period
        if not df.empty and 'period' in df.columns and df['period'].notna().any():
            value_by_period = df.groupby('period')['approximate_value'].sum().nlargest(10).sort_values()
            if not value_by_period.empty:
                fig2, ax2 = plt.subplots(figsize=(10, 6))
                fig2.patch.set_facecolor(bg_color)
                ax2.set_facecolor(bg_color)
                value_by_period.plot(kind='barh', ax=ax2, color=bar_color)
                ax2.set_title('Top 10 Collection Value by Period', fontsize=16, color=text_color, pad=20)
                ax2.set_xlabel('Total Approximate Value (£)', color=text_color)
                ax2.set_ylabel('Period', color=text_color)
                ax2.tick_params(colors=text_color)
                ax2.spines['top'].set_visible(False); ax2.spines['right'].set_visible(False)
                plt.tight_layout()
                img2 = io.BytesIO()
                plt.savefig(img2, format='png', bbox_inches='tight')
                img2.seek(0)
                plot_url2 = base64.b64encode(img2.getvalue()).decode()
                plt.close(fig2)

        return render_template('collection_dashboard.html', 
                               stats=stats, 
                               plot_url1=plot_url1, 
                               plot_url2=plot_url2,
                               filters=current_filters,
                               item_types=item_types,
                               periods=periods)

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
            # --- Form Data Retrieval ---
            name = request.form.get('name')
            item_type = request.form.get('item_type').strip() if request.form.get('item_type') else None
            period = request.form.get('period').strip() if request.form.get('period') else None
            description = request.form.get('description')
            provenance = request.form.get('provenance')
            value_str = request.form.get('approximate_value')
            is_sellable = 'is_sellable' in request.form 
            
            if not name:
                flash('Item Name is a required field.', 'error')
                return redirect(url_for('add_collection_item'))

            approximate_value = float(value_str) if value_str else None
            
            # --- File Upload ---
            image_url = None
            image_file = request.files.get('image')
            if image_file and image_file.filename != '':
                if GCS_BUCKET_NAME:
                    image_url = upload_to_gcs(image_file, GCS_BUCKET_NAME)
                    if not image_url:
                        flash('Image upload failed. Please try again.', 'error')
                        return redirect(url_for('add_collection_item'))
                else:
                    flash('Image upload is not configured on the server.', 'error')

            # --- Database Insertion ---
            sql = """
                INSERT INTO antiques (name, item_type, period, description, provenance, approximate_value, is_sellable, image_url, user_id, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, NOW(), NOW())
            """
            with conn.cursor() as cur:
                cur.execute(sql, (
                    name, item_type, period, description, provenance, 
                    approximate_value, is_sellable, image_url
                ))
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

    # --- GET Request Logic ---
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT item_type FROM antiques WHERE item_type IS NOT NULL AND item_type != '' ORDER BY item_type")
            item_types = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT DISTINCT period FROM antiques WHERE period IS NOT NULL AND period != '' ORDER BY period")
            periods = [row[0] for row in cur.fetchall()]
    except Exception as e:
        log_activity('error', details={"function": "add_collection_item_get", "error": str(e)})
        flash("Error fetching suggestions.", "error")
        item_types = []
        periods = []

    return render_template('add_item.html', item_types=item_types, periods=periods)

# --- New Routes for Editing and Deleting Collection Items ---

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

            # --- Form Data Retrieval ---
            approximate_value = float(request.form.get('approximate_value')) if request.form.get('approximate_value') else None
            
            # --- Handle Image Update ---
            image_url = item['image_url'] # Keep old image by default
            image_file = request.files.get('image')
            if image_file and image_file.filename != '':
                if GCS_BUCKET_NAME:
                    if item['image_url']: # Delete old image if it exists
                        delete_from_gcs(item['image_url'], GCS_BUCKET_NAME)
                    
                    new_image_url = upload_to_gcs(image_file, GCS_BUCKET_NAME)
                    if new_image_url:
                        image_url = new_image_url
                    else:
                        flash('New image upload failed. Please try again.', 'error')
                        return redirect(url_for('edit_collection_item', item_id=item_id))
                else:
                    flash('Image upload is not configured on the server.', 'error')

            # --- Database Update ---
            sql = """
                UPDATE antiques SET 
                name=%s, item_type=%s, period=%s, description=%s, provenance=%s, 
                approximate_value=%s, is_sellable=%s, image_url=%s, updated_at=NOW()
                WHERE id=%s AND user_id=1
            """
            with conn.cursor() as cur:
                cur.execute(sql, (
                    name, request.form.get('item_type').strip() if request.form.get('item_type') else None,
                    request.form.get('period').strip() if request.form.get('period') else None,
                    request.form.get('description'), request.form.get('provenance'),
                    approximate_value, 'is_sellable' in request.form, image_url, item_id
                ))
            conn.commit()

            flash(f"Item '{name}' has been updated!", 'success')
            log_activity('collection_item_updated', details={'item_id': item_id, 'name': name})
            return redirect(url_for('view_collection_item', item_id=item_id))

        # --- GET Request Logic (fetch suggestions for datalists) ---
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

        if item['image_url']: # Delete image from GCS
            delete_from_gcs(item['image_url'], GCS_BUCKET_NAME)

        with conn.cursor() as cur: # Delete item from database
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
        if ORACLE_API_FUNCTION_KEY:
            headers["X-Api-Key"] = ORACLE_API_FUNCTION_KEY

        response = requests.post(ORACLE_API_ENDPOINT_URL, json=payload, headers=headers, timeout=300)
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
    except requests.exceptions.Timeout:
        print(f"[ERROR] Timeout error for job {job_id}")
        traceback.print_exc()
        error_message = "The Oracle took more than 5 minutes to respond. The request has been cancelled. Please try a simpler question or try again later."
        oracle_jobs[job_id] = {"status": "error", "reply": error_message}
        log_activity(
            'oracle_api_error',
            details={'job_id': job_id, 'error': 'Timeout after 300 seconds'},
            ip_address=ip_address, user_agent=user_agent, path=path
        )
    except Exception as e:
        print(f"[ERROR] Background thread error for job {job_id}: {e}")
        traceback.print_exc()
        error_message = f"The Oracle could not respond due to an unexpected error. Details: {str(e)}"
        oracle_jobs[job_id] = {"status": "error", "reply": error_message}
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
    
    payload = { "message": client_data.get('message'), "history": client_data.get('history', []) }
    
    thread = threading.Thread(
        target=run_oracle_query_in_background,
        args=(job_id, payload, request.remote_addr, request.headers.get('User-Agent'), request.path)
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
        return jsonify({"status": "error", "reply": "Job not found. It may have been cleared from memory."}), 404
        
    if job['status'] == 'complete' or job['status'] == 'error':
        return jsonify(oracle_jobs.pop(job_id))
    else:
        return jsonify(job)

# --- Other Routes ---
@app.route('/db_test')
@login_required
def db_test():
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            db_version = cur.fetchone()
        return f"Database connection successful!<br/>PostgreSQL version: {db_version[0]}"
    except Exception as e:
        return f"Database connection failed: {e}", 500

@app.route('/admin/activity_log')
@login_required
def view_activity_log():
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, user_id, activity_type, ip_address, path, details, 
                       TO_CHAR(timestamp, 'YYYY-MM-DD HH24:MI:SS TZ') as formatted_timestamp 
                FROM activity_log 
                ORDER BY timestamp DESC 
                LIMIT 100
            """)
            activities = cur.fetchall()

        return render_template('activity_log.html', activities=activities)
    except Exception as e:
        log_activity('error', details={"function": "view_activity_log", "error": str(e)})
        traceback.print_exc()
        flash("Error fetching activity log.", "error")
        return redirect(url_for('hello'))
    
# --- Other Routes ---
@app.route('/images/<path:filename>')
@login_required
def serve_private_image(filename):
    if not GCS_BUCKET_NAME or not GOOGLE_CREDENTIALS_JSON:
        return "Image serving is not configured.", 500

    try:
        credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        storage_client = storage.Client.from_service_account_info(credentials_info)
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(filename)

        if not blob.exists():
            return "Image not found.", 404
            
        signed_url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=15),
            method="GET",
        )
        
        return redirect(signed_url)
        
    except Exception as e:
        log_activity('error', details={"function": "serve_private_image", "error": str(e)})
        traceback.print_exc()
        return "Error serving image.", 500
        

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5167)), debug=False)