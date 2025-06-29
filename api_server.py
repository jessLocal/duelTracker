# api_server.py
# A simple web server to bridge the Discord bot and the Roblox client.
# Now ONLY includes endpoints for managing spectate duel trace requests and a simple 'trigger'.

from flask import Flask, jsonify, request
import logging
import threading
import os 

# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv()

# --- CONFIGURATION ---
# IMPORTANT: This key MUST match what's set in your .env file and Discord bot.
# On Render, this env var will be directly set in the Render dashboard.
DISCORD_BOT_API_KEY = os.getenv('DISCORD_BOT_API_KEY', 'DEFAULT_DISCORD_BOT_API_KEY') 

# For Render, Flask typically listens on 0.0.0.0 and a port provided by the environment.
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

# --- Global state for the 'trigger' command (in-memory, will reset on server restart) ---
# This simple boolean flag will tell the Roblox client to perform a test action.
is_trigger_pending = False
trigger_lock = threading.Lock() # To safely update the trigger state

# --- SECURITY HELPER ---
def verify_discord_bot_api_key(provided_key, expected_key):
    """Verifies if the provided API key from Discord bot matches the expected key."""
    if provided_key != expected_key:
        logging.warning(f"Unauthorized Discord bot API access attempt from IP: {request.remote_addr} with key: '{provided_key}' (expected: '{expected_key}')")
        return False
    return True

# --- API ENDPOINTS (Duel Tracing) ---

@app.route('/request_trace', methods=['POST'])
def request_trace():
    """
    Handles POST requests from the Discord bot to request a duel trace.
    Requires DISCORD_BOT_API_KEY.
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    
    data = request.get_json()
    provided_key = data.get('api_key')
    if not verify_discord_bot_api_key(provided_key, DISCORD_BOT_API_KEY):
        return jsonify({"error": "Invalid API Key"}), 401

    player1_id = data.get('player1Id', type=int)
    player2_id = data.get('player2Id', type=int)
    requester_discord_id = data.get('requester_discord_id', type=str)

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
    Includes the 'trigger' status.
    """
    client_id = request.args.get('client_id', type=int)
    client_name = request.args.get('client_name', type=str)

    if not client_id or not client_name:
        return jsonify({"error": "client_id and client_name are required"}), 400

    current_trace = None
    with trace_lock:
        current_trace = active_trace_request

    current_trigger_status = False
    with trigger_lock:
        current_trigger_status = is_trigger_pending

    response_data = {
        "status": "idle", # Default to idle for trace status
        "triggerPending": current_trigger_status
    }

    if current_trace:
        response_data["status"] = "tracing"
        response_data["targetPlayer1Id"] = current_trace["player1Id"]
        response_data["targetPlayer2Id"] = current_trace["player2Id"]
        response_data["requester_discord_id"] = current_trace["requester_discord_id"]
        logging.info(f"Roblox client {client_name} ({client_id}) requested trace target. Providing: {current_trace['player1Id']}, {current_trace['player2Id']}.")
    else:
        logging.info(f"Roblox client {client_name} ({client_id}) requested trace target. No active trace.")
    
    return jsonify(response_data)

@app.route('/trace_complete', methods=['POST'])
def trace_complete():
    """
    Handles POST requests from the Roblox script to signal that a trace is complete.
    'status' can be 'completed' or 'aborted'.
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    
    data = request.get_json()
    client_id = data.get('clientId', type=int)
    status = data.get('status', type=str) 
    duel_id = data.get('duelId', type=str)
    reason = data.get('reason', type=str)

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

# --- API ENDPOINTS (Trigger) ---

@app.route('/set_trigger', methods=['POST'])
def set_trigger():
    """
    Handles POST requests from the Discord bot to set the 'trigger' flag.
    Requires DISCORD_BOT_API_KEY.
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    
    data = request.get_json()
    provided_key = data.get('api_key')
    if not verify_discord_bot_api_key(provided_key, DISCORD_BOT_API_KEY):
        return jsonify({"error": "Invalid API Key"}), 401

    with trigger_lock:
        global is_trigger_pending
        is_trigger_pending = True
    
    logging.info("Received 'set_trigger' request. Trigger is now pending.")
    return jsonify({"success": True, "message": "Trigger set successfully."})

@app.route('/clear_trigger', methods=['POST'])
def clear_trigger():
    """
    Handles POST requests from the Roblox script to clear the 'trigger' flag after action.
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    
    data = request.get_json()
    client_id = data.get('clientId', type=int)
    client_name = data.get('clientName', type=str)

    if not client_id or not client_name:
        return jsonify({"error": "clientId and clientName are required"}), 400

    with trigger_lock:
        global is_trigger_pending
        if is_trigger_pending:
            is_trigger_pending = False
            logging.info(f"Roblox client {client_name} ({client_id}) reported trigger cleared. Trigger is now reset.")
            return jsonify({"success": True, "message": "Trigger cleared successfully."})
        else:
            logging.warning(f"Roblox client {client_id} reported trigger cleared, but no trigger was pending.")
            return jsonify({"success": False, "message": "No trigger was pending to clear."}), 404


if __name__ == '__main__':
    print("--- Duel Tracing and Trigger API Server ---")
    print(f"Starting server on {HOST}:{PORT}")
    print("Endpoints available:")
    print("  /request_trace (POST) - For Discord bot to request a duel trace")
    print("  /get_trace_target (GET) - For Roblox client to poll for trace targets (includes trigger status)")
    print("  /trace_complete (POST) - For Roblox client to report trace completion")
    print("  /set_trigger (POST) - For Discord bot to set a 'trigger' for Roblox client")
    print("  /clear_trigger (POST) - For Roblox client to clear the 'trigger'")
    print("IMPORTANT: Make sure your DISCORD_BOT_API_KEY is set in your .env file and is secret.")
    app.run(host=HOST, port=PORT)
