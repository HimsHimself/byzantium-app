from flask import Flask
import os

# Create an instance of the Flask class
app = Flask(__name__)

# The default route to show a welcome message
@app.route('/')
def hello():
    return "Hello from Byzantium! The web service is running."

# A route to test database connection (we'll make this work later)
@app.route('/db_test')
def db_test():
    # This will fail for now, but we'll connect it properly soon
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return f"Database URL is configured."
    else:
        return "Database URL is NOT configured."

# This part is not needed by Gunicorn but is useful for local testing
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)