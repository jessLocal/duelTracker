# api_server.py
# A simple web server to bridge the Roblox game and the Discord bot's database.
# Now includes endpoints for managing spectate duel trace requests.

from flask import Flask, jsonify, request
import sqlite3
import logging
import threading
import os 

# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv()

# --- CONFIGURATION ---
# For Render, database file usually needs to be in a persistent volume or use an external DB.
# For simplicity in this example, we'll use an in-memory SQLite for active_trace_request,
# and a file-based SQLite for robux_donations.db which might be transient on Render's free tier.
# For production, consider Render Disk or a dedicated database service (e.g., PostgreSQL).
DATABASE_FILE = 'robux_donations.db' # Existing database for robux donations

# IMPORTANT: These keys MUST match what's set in your .env file and Roblox script/Discord bot.
# On Render, these env vars will be directly set in the Render dashboard.
API_KEY = os.getenv('API_KEY', 'DEFAULT_ROBLOX_API_KEY') 
DISCORD_BOT_API_KEY = os.getenv('DISCORD_BOT_API_KEY', 'DEFAULT_DISCORD_BOT_API_KEY') 

# For Render, Flask typically listens on 0.0.0.0 and a port provided by the environment.
# We'll use os.getenv("PORT") which Render automatically provides.
HOST = '0.0.0.0'
PORT = int(os.getenv('PORT', 3000)) # Render provides the PORT env var

# --- SETUP ---
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Global state for tracing requests (in-memory, will reset on server restart) ---
# This will store the target players' IDs for the Roblox client to spectate
# Structure: {"player1Id": int, "player2Id": int, "requester_discord_id": str} or None
active_trace_request = None 
trace_lock = threading.Lock() # To safely update the active_trace_request from multiple threads

def get_db_connection():
    """Establishes a connection to the SQLite database."""
    # The check_same_thread=False is important for Flask to handle threads correctly.
    # On Render, this might mean a new connection for each request if not using a connection pool.
    conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# --- SECURITY HELPER ---
def verify_api_key(provided_key, expected_key):
    """Verifies if the provided API key matches the expected key."""
    if provided_key != expected_key:
        logging.warning(f"Unauthorized API access attempt from IP: {request.remote_addr} with key: '{provided_key}' (expected: '{expected_key}')")
        return False
    return True

# --- API ENDPOINTS (Existing) ---

@app.route('/get_balance', methods=['GET'])
def get_balance():
    """
    Handles GET requests from the Roblox game to get a player's balance.
    Expects 'roblox_id' and 'api_key' as query parameters.
    """
    provided_key = request.args.get('api_key')
    if not verify_api_key(provided_key, API_KEY):
        return jsonify({"error": "Invalid API Key"}), 401

    roblox_id_str = request.args.get('roblox_id')
    if not roblox_id_str:
        return jsonify({"error": "roblox_id parameter is missing"}), 400
    try:
        roblox_id = int(roblox_id_str)
    except ValueError:
        return jsonify({"error": "roblox_id parameter must be an integer"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM users WHERE linked_roblox_id = ?", (roblox_id,))
        user_data = cursor.fetchone()
        conn.close()

        if user_data:
            balance = user_data['balance']
            logging.info(f"GET /get_balance: Successfully retrieved balance for Roblox ID {roblox_id}: {balance}")
            return jsonify({"success": True, "balance": balance})
        else:
            logging.info(f"GET /get_balance: No data found for Roblox ID {roblox_id}. Returning default balance of 0.")
            return jsonify({"success": True, "balance": 0})

    except Exception as e:
        logging.error(f"GET /get_balance: Error for Roblox ID {roblox_id}: {e}")
        return jsonify({"error": "An internal server error occurred"}), 500


@app.route('/update_balance', methods=['POST'])
def update_balance():
    """
    Handles POST requests from the Roblox game to add to a player's balance after a purchase.
    Expects a JSON body with 'roblox_id', 'amount_to_add', and 'api_key'.
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
        
    data = request.get_json()
    provided_key = data.get('api_key')
    if not verify_api_key(provided_key, API_KEY):
        return jsonify({"error": "Invalid API Key"}), 401

    roblox_id_str = data.get('roblox_id')
    amount_to_add_str = data.get('amount_to_add')

    if not roblox_id_str or not amount_to_add_str:
        return jsonify({"error": "roblox_id and amount_to_add are required"}), 400

    try:
        roblox_id = int(roblox_id_str)
        amount_to_add = int(amount_to_add_str)
    except ValueError:
        return jsonify({"error": "roblox_id and amount_to_add must be integers"}), 400

    if amount_to_add <= 0:
        return jsonify({"error": "amount_to_add must be a positive value"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("BEGIN TRANSACTION;")
        cursor.execute("SELECT balance FROM users WHERE linked_roblox_id = ?", (roblox_id,))
        user_exists = cursor.fetchone()

        if user_exists:
            cursor.execute(
                "UPDATE users SET balance = balance + ? WHERE linked_roblox_id = ?",
                (amount_to_add, roblox_id)
            )
            logging.info(f"POST /update_balance: Updated balance for Roblox ID {roblox_id}. Added {amount_to_add}.")
        else:
            logging.warning(f"POST /update_balance: Attempted to update balance for non-existent user with Roblox ID {roblox_id}.")

        conn.commit()
        return jsonify({"success": True, "message": "Balance update processed."})

    except Exception as e:
        logging.error(f"POST /update_balance: Error for Roblox ID {roblox_id}: {e}")
        if conn:
            conn.rollback()
        return jsonify({"error": "An internal server error occurred"}), 500
    finally:
        if conn:
            conn.close()

# --- API ENDPOINTS (New for Duel Tracing) ---

@app.route('/request_trace', methods=['POST'])
def request_trace():
    """
    Handles POST requests from the Discord bot to request a duel trace.
    Expects a JSON body with 'player1Id', 'player2Id', and 'api_key'.
    Optionally, 'requester_discord_id' can be included for tracking.
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    
    data = request.get_json()
    provided_key = data.get('api_key')
    if not verify_api_key(provided_key, DISCORD_BOT_API_KEY):
        return jsonify({"error": "Invalid API Key"}), 401

    player1_id = data.get('player1Id', type=int)
    player2_id = data.get('player2Id', type=int)
    requester_discord_id = data.get('requester_discord_id', type=str) # Optional

    if not player1_id or not player2_id:
        return jsonify({"error": "player1Id and player2Id are required"}), 400

    if player1_id == player2_id:
        return jsonify({"error": "Player1Id and Player2Id must be different"}), 400

    with trace_lock:
        global active_trace_request
        if active_trace_request:
            logging.warning(f"Trace request for {player1_id}, {player2_id} received, but another trace is already active.")
            return jsonify({
                "success": False, 
                "message": "Another duel trace is already active. Please wait or clear the current trace."
            }), 409 # Conflict
        
        active_trace_request = {
            "player1Id": player1_id,
            "player2Id": player2_id,
            "requester_discord_id": requester_discord_id,
            "status": "pending_roblox_client_pickup"
        }
    
    logging.info(f"Received trace request for Roblox IDs: {player1_id} and {player2_id} (Requester: {requester_discord_id or 'N/A'})")
    return jsonify({"success": True, "message": "Trace request received and set."})


@app.route('/get_trace_target', methods=['GET'])
def get_trace_target():
    """
    Handles GET requests from the Roblox script to get the current trace target.
    Expects 'client_id' and 'client_name' as query parameters.
    """
    client_id = request.args.get('client_id', type=int)
    client_name = request.args.get('client_name', type=str)

    if not client_id or not client_name:
        return jsonify({"error": "client_id and client_name are required"}), 400

    with trace_lock:
        if active_trace_request:
            logging.info(f"Roblox client {client_name} ({client_id}) requested trace target. Providing: {active_trace_request['player1Id']}, {active_trace_request['player2Id']}.")
            return jsonify({
                "status": "tracing",
                "targetPlayer1Id": active_trace_request["player1Id"],
                "targetPlayer2Id": active_trace_request["player2Id"],
                "requester_discord_id": active_trace_request["requester_discord_id"]
            })
        else:
            logging.info(f"Roblox client {client_name} ({client_id}) requested trace target. No active trace.")
            return jsonify({"status": "idle"})

@app.route('/trace_complete', methods=['POST'])
def trace_complete():
    """
    Handles POST requests from the Roblox script to signal that a trace is complete.
    Expects a JSON body with 'clientId', 'status', and 'duelId'.
    'status' can be 'completed' or 'aborted'.
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    
    data = request.get_json()
    client_id = data.get('clientId', type=int)
    status = data.get('status', type=str) # 'completed', 'aborted'
    duel_id = data.get('duelId', type=str)
    reason = data.get('reason', type=str) # Optional reason for 'aborted' status

    if not client_id or not status:
        return jsonify({"error": "clientId and status are required"}), 400

    with trace_lock:
        global active_trace_request
        if active_trace_request:
            logging.info(f"Roblox client {client_id} reported trace {status} for duel {duel_id}. Reason: {reason or 'N/A'}")
            active_trace_request = None # Clear the trace request
            return jsonify({"success": True, "message": "Trace status received and cleared."})
        else:
            logging.warning(f"Roblox client {client_id} reported trace {status} for duel {duel_id}, but no active trace was found.")
            return jsonify({"success": False, "message": "No active trace to complete."}), 404 # Not Found

if __name__ == '__main__':
    # Ensure the database is set up when the server starts
    # Note: On Render free tier, this SQLite file might reset with each deployment or restart.
    # For persistent data, consider Render's Persistent Disks or an external database like PostgreSQL.
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            discord_id INTEGER PRIMARY KEY,
            discord_username TEXT NOT NULL,
            phrase TEXT,
            linked_roblox_id INTEGER,
            linked_roblox_username TEXT,
            balance INTEGER DEFAULT 0,
            session_expiry_timestamp REAL
        )
    ''')
    try:
        cursor.execute('ALTER TABLE users ADD COLUMN session_expiry_timestamp REAL;')
    except sqlite3.OperationalError:
        pass # Column already exists
    conn.commit()
    conn.close()
    logging.info("Database setup complete.")


    print("--- Roblox-Discord DB Bridge API (v2) with Duel Tracing ---")
    print(f"Starting server on {HOST}:{PORT}")
    print("Endpoints available:")
    print("  /get_balance (GET) - For Roblox leaderstats")
    print("  /update_balance (POST) - For Roblox leaderstats")
    print("  /request_trace (POST) - For Discord bot to request a duel trace")
    print("  /get_trace_target (GET) - For Roblox client to poll for trace targets")
    print("  /trace_complete (POST) - For Roblox client to report trace completion")
    print("IMPORTANT: Make sure your API_KEY and DISCORD_BOT_API_KEY are set in your .env file and are secret.")
    # For Render, use the PORT environment variable provided by Render.
    app.run(host=HOST, port=PORT)
