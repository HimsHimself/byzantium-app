import os
import psycopg2
import json
import requests
import uuid
import threading
import pytz
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
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME") # Should be 'byzantium_bucket'
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

# --- GCS Upload Helper (Render-compatible) ---
def upload_to_gcs(file_to_upload, bucket_name):
    """Uploads a file to a given GCS bucket and returns its public URL."""
    if not file_to_upload or not file_to_upload.filename:
        return None
    
    # Authenticate using the JSON credentials stored in the environment variable
    credentials_json_str = os.environ.get('GOOGLE_CREDENTIALS_JSON')
    if not credentials_json_str:
        print("ERROR: GOOGLE_CREDENTIALS_JSON environment variable not set.")
        return None
    
    try:
        credentials_info = json.loads(credentials_json_str)
        storage_client = storage.Client.from_service_account_info(credentials_info)
    except json.JSONDecodeError:
        print("ERROR: Could not decode GOOGLE_CREDENTIALS_JSON.")
        return None
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
        return blob.public_url
    except Exception as e:
        print(f"Error uploading to GCS: {e}")
        traceback.print_exc()
        return None

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
       request.endpoint not in ['login', 'static', 'logout', 'api_oracle_chat_start', 'api_oracle_chat_status']:
        log_activity('pageview')

@app.route('/')
@login_required
def hello():
    return render_template('index.html')

# --- Notes and Folders Routes ---
# ... (existing notes and folders routes are unchanged) ...
@app.route('/notes/')
@app.route('/notes/folder/<int:folder_id>')
@login_required
def notes_page(folder_id=None):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        current_folder = None
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
            
        cur.close()
        return render_template('notes.html', folders=folders_to_display, notes_in_folder=notes_in_current_folder, current_folder=current_folder, current_note=None)
    except Exception as e:
        traceback.print_exc()
        flash("Error loading notes.", "error")
        return redirect(url_for('hello'))

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
            redirect_url = url_for('notes_page', folder_id=new_folder_id)
        except Exception as e:
            conn.rollback()
            log_activity('folder_create_error', details={'folder_name': folder_name, 'error': str(e)})
            flash(f"Error: {e}", 'error')
    return redirect(redirect_url)

@app.route('/folder/<int:folder_id>/rename', methods=['POST'])
@login_required
def rename_folder(folder_id):
    new_folder_name = request.form.get('new_folder_name','').strip()
    conn = get_db()
    try:
        if not new_folder_name: 
            flash('Folder name cannot be empty.', 'error')
            return redirect(url_for('notes_page', folder_id=folder_id))
        
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT parent_folder_id FROM folders WHERE id = %s AND user_id = 1", (folder_id,))
            folder_data = cur.fetchone()
            if not folder_data:
                flash("Folder not found.", "error")
                return redirect(url_for('notes_page'))
            
            cur.execute("UPDATE folders SET name = %s, updated_at = NOW() WHERE id = %s AND user_id = 1", (new_folder_name, folder_id))
        conn.commit()
        
        log_activity('folder_renamed', details={'folder_id': folder_id, 'new_name': new_folder_name})
        flash(f"Folder renamed to '{new_folder_name}'.", 'success')
        parent_id = folder_data['parent_folder_id']
        redirect_url = url_for('notes_page', folder_id=parent_id) if parent_id else url_for('notes_page')
        return redirect(redirect_url)

    except Exception as e:
        conn.rollback()
        log_activity('folder_rename_error', details={'folder_id': folder_id, 'error': str(e)})
        flash(f"Error: {e}", 'error')
        return redirect(url_for('notes_page', folder_id=folder_id))

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
                cur.execute("INSERT INTO notes (title, content, folder_id, user_id, created_at, updated_at) VALUES (%s, %s, %s, 1, NOW(), NOW()) RETURNING id",(note_title, '', folder_id))
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

            folder_id = current_note['folder_id']
            cur.execute("SELECT * FROM folders WHERE id = %s AND user_id = 1", (folder_id,))
            current_folder = cur.fetchone()
            
            parent_id_for_folder_list = current_folder['parent_folder_id'] if current_folder else None
            if parent_id_for_folder_list:
                cur.execute("SELECT * FROM folders WHERE parent_folder_id = %s AND user_id = 1 ORDER BY name", (parent_id_for_folder_list,))
            else:
                 cur.execute("SELECT * FROM folders WHERE parent_folder_id IS NULL AND user_id = 1 ORDER BY name")
            folders_to_display = cur.fetchall()
            
            cur.execute("SELECT id, title, updated_at FROM notes WHERE folder_id = %s AND user_id = 1 ORDER BY updated_at DESC", (folder_id,))
            notes_in_same_folder = cur.fetchall()
        
        return render_template('notes.html', folders=folders_to_display, current_folder=current_folder, notes_in_folder=notes_in_same_folder, current_note=current_note)
    except Exception as e:
        traceback.print_exc()
        flash("Error viewing note.", "error")
        return redirect(url_for('notes_page'))

@app.route('/note/<int:note_id>/rename', methods=['POST'])
@login_required
def rename_note(note_id):
    new_note_title = request.form.get('new_note_title','').strip()
    redirect_url = url_for('view_note', note_id=note_id)
    if not new_note_title: 
        flash("Note title cannot be empty.", "error")
    else:
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id FROM notes WHERE id = %s AND user_id = 1", (note_id,))
                if not cur.fetchone(): 
                    flash("Note not found.", "error")
                    return redirect(url_for('notes_page'))
                cur.execute("UPDATE notes SET title = %s, updated_at = NOW() WHERE id = %s AND user_id = 1",(new_note_title, note_id))
            conn.commit()
            log_activity('note_renamed', details={'note_id': note_id, 'new_title': new_note_title})
            flash('Note renamed.', 'success')
        except Exception as e:
            conn.rollback()
            log_activity('note_rename_error', details={'note_id': note_id, 'error': str(e)})
            flash(f"Error: {e}", 'error')
    return redirect(redirect_url)

@app.route('/note/<int:note_id>/update', methods=['POST'])
@login_required
def update_note(note_id):
    new_content = request.form.get('note_content', '')
    redirect_url = url_for('view_note', note_id=note_id)
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT title FROM notes WHERE id = %s AND user_id = 1", (note_id,))
            note_data = cur.fetchone()
            if not note_data: 
                flash("Note not found.", "error")
                return redirect(url_for('notes_page'))
            cur.execute("UPDATE notes SET content = %s, updated_at = NOW() WHERE id = %s AND user_id = 1", (new_content, note_id))
        conn.commit()
        log_activity('note_updated', details={'note_id': note_id, 'note_title': note_data['title']})
        flash('Note updated.', 'success')
    except Exception as e:
        conn.rollback()
        log_activity('note_update_error', details={'note_id': note_id, 'error': str(e)})
        flash(f"Error: {e}", 'error')
    return redirect(redirect_url)

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
        london_tz = pytz.timezone("Europe/London")
        today_london = datetime.now(london_tz).date()
        thirty_days_ago = datetime.now() - timedelta(days=30)
        
        today_total_calories = 0

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM food_log WHERE user_id = 1 AND log_time >= %s ORDER BY log_time DESC", (thirty_days_ago,))
            logs = cur.fetchall()

            sql_today_calories = """
                SELECT SUM(calories) as total
                FROM food_log
                WHERE user_id = 1 AND DATE(log_time AT TIME ZONE 'Europe/London') = %s;
            """
            cur.execute(sql_today_calories, (today_london,))
            result = cur.fetchone()
            if result and result['total'] is not None:
                today_total_calories = int(result['total'])
        
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
            
            plot_path = os.path.join('static', 'calories_chart.png')
            if not os.path.exists('static'):
                os.makedirs('static')
            plt.savefig(plot_path)
            chart_url = url_for('static', filename='calories_chart.png')
        else:
            chart_url = None

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
            cur.execute("SELECT DISTINCT item_type FROM antiques WHERE item_type IS NOT NULL ORDER BY item_type")
            item_types = [row[0] for row in cur.fetchall()]
    except Exception as e:
        log_activity('error', details={"function": "add_collection_item_get", "error": str(e)})
        flash("Error fetching item types.", "error")
        item_types = []

    return render_template('add_item.html', item_types=item_types)


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
            cur.execute("SELECT id, user_id, activity_type, ip_address, path, details, TO_CHAR(timestamp, 'YYYY-MM-DD HH24:MI:SS TZ') as formatted_timestamp FROM activity_log ORDER BY timestamp DESC LIMIT 100")
            activities = cur.fetchall()

        dashboard_url = url_for('hello')
        html_parts = [
            '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Activity Log</title>',
            '<script src="https://cdn.tailwindcss.com"></script>',
            '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">',
            "<style> body { font-family: 'Inter', sans-serif; background-color: #f8fafc; color: #334155; padding: 20px; }",
            "table { width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 0.9em; }",
            "th, td { border: 1px solid #cbd5e1; text-align: left; vertical-align: top; word-break: break-word; }",
            "th { color: #4B0082; }",
            "a { color: #4B0082; text-decoration: none; } a:hover { text-decoration: underline; }",
            ".details-json { max-width: 400px; max-height: 200px; overflow: auto; white-space: pre-wrap; background-color: #eef2ff; padding: 5px; border-radius: 4px; font-family: monospace; font-size: 0.8em;}</style>",
            '</head><body>',
            f'<h1 class="text-2xl font-semibold mb-4" style="color: #4B0082;">Activity Log <span style="color: #DAA520;">&dagger;</span> (Last 100)</h1>',
            f'<p><a href="{dashboard_url}">Back to Dashboard</a></p><div class="overflow-x-auto"><table class="w-full text-sm text-left text-slate-500">'
        ]

        if activities:
            colnames = list(activities[0].keys())
            html_parts.append('<thead class="text-xs text-slate-700 uppercase bg-slate-100"><tr>')
            for name in colnames:
                html_parts.append(f"<th scope=\"col\" class=\"px-6 py-3\">{name.replace('_', ' ').title()}</th>")
            html_parts.append('</tr></thead><tbody>')

            for activity in activities:
                html_parts.append('<tr class="bg-white border-b hover:bg-slate-50">')
                for col_name in colnames:
                    value = activity.get(col_name)
                    if col_name == 'details' and value is not None:
                        details_json = json.dumps(value, indent=2)
                        html_parts.append(f"<td class=\"px-6 py-2\"><pre class='details-json'>{details_json}</pre></td>")
                    else:
                        html_parts.append(f"<td class=\"px-6 py-2\">{str(value) if value is not None else ''}</td>")
                html_parts.append('</tr>')
            html_parts.append("</tbody>")
        else:
            html_parts.append("<thead class=\"text-xs text-slate-700 uppercase bg-slate-100\"><tr><th>Info</th></tr></thead>")
            html_parts.append("<tbody><tr><td colspan=\"1\" class=\"px-6 py-4 text-center text-slate-500 italic\">No activities found.</td></tr></tbody>")
            
        html_parts.append('</table></div></body></html>')
        return "".join(html_parts)
    except Exception as e:
        log_activity('error', details={"function": "view_activity_log", "error": str(e)})
        return f"Error fetching activity log: {e}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5167)), debug=False)