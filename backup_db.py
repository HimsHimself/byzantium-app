import os
import subprocess
import json
from datetime import datetime
from google.cloud import storage
import io

# --- Configuration ---
# These are the same environment variables your main Flask app uses.
DB_URL = os.environ.get("DATABASE_URL")
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

def main():
    """
    Main function to run the database backup and upload process.
    """
    print("--- Starting database backup process ---")

    # 1. Validate environment variables
    if not all([DB_URL, GCS_BUCKET_NAME, GOOGLE_CREDENTIALS_JSON]):
        print("[ERROR] Missing one or more required environment variables (DATABASE_URL, GCS_BUCKET_NAME, GOOGLE_CREDENTIALS_JSON).")
        return

    try:
        # 2. Set up GCS client
        try:
            credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)
            storage_client = storage.Client.from_service_account_info(credentials_info)
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            print(f"Successfully connected to GCS bucket: '{GCS_BUCKET_NAME}'")
        except Exception as e:
            print(f"[ERROR] Failed to create GCS client: {e}")
            return

        # 3. Define the backup command and filename
        timestamp = datetime.utcnow().strftime('%Y-%m-%d_%H-%M-%S')
        backup_filename = f"db_backup_{timestamp}.dump"
        
        # Using pg_dump with the database URL.
        # -Fc: Custom format, compressed and flexible.
        # --no-owner: Improves portability by not tying objects to the original owner.
        # --clean: Adds commands to clean (drop) database objects before recreating.
        command = [
            'pg_dump',
            '--format=custom',
            '--no-owner',
            '--clean',
            DB_URL
        ]
        
        print(f"Running pg_dump and streaming to '{backup_filename}' in GCS...")

        # 4. Run pg_dump and stream the output to GCS
        # We use a pipe to capture the output of pg_dump without saving it to a local file.
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # The backup content is streamed directly from the command's output.
        backup_blob = bucket.blob(f"database_backups/{backup_filename}")
        
        # Use io.BytesIO to handle the stream in memory
        with io.BytesIO(process.stdout.read()) as backup_stream:
            backup_stream.seek(0) # Go to the beginning of the stream
            backup_blob.upload_from_file(backup_stream, content_type='application/octet-stream')

        # Wait for the process to complete and check for errors
        _, stderr = process.communicate()
        if process.returncode != 0:
            error_message = stderr.decode('utf-8').strip()
            print(f"[ERROR] pg_dump failed with return code {process.returncode}.")
            print(f"Error details: {error_message}")
            return
            
        print(f"--- Successfully uploaded backup to GCS: {backup_blob.name} ---")

        # 5. (Optional) Implement a retention policy
        cleanup_old_backups(bucket, days_to_keep=30)

    except Exception as e:
        print(f"[CRITICAL] An unexpected error occurred during the backup process: {e}")

def cleanup_old_backups(bucket, days_to_keep):
    """
    Deletes backups in the 'database_backups/' folder older than the specified number of days.
    """
    print(f"--- Running cleanup task: Deleting backups older than {days_to_keep} days ---")
    try:
        blobs = bucket.list_blobs(prefix="database_backups/")
        now = datetime.utcnow()
        for blob in blobs:
            if blob.time_created:
                # GCS time_created is timezone-aware, so make 'now' offset-aware for comparison.
                blob_age = datetime.now(blob.time_created.tzinfo) - blob.time_created
                if blob_age.days > days_to_keep:
                    print(f"Deleting old backup: {blob.name} (Age: {blob_age.days} days)")
                    blob.delete()
        print("--- Cleanup task complete ---")
    except Exception as e:
        print(f"[WARNING] An error occurred during backup cleanup: {e}")


if __name__ == '__main__':
    main()