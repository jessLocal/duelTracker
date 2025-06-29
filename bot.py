# bot.py
# Overhauled Discord bot to interact with the external API server for duel tracing and a new 'trigger' command.
# Designed to be run on a platform like Render or locally.

import discord
from discord.ext import commands
from discord import app_commands # For slash commands
import os
from dotenv import load_dotenv
import requests # For making HTTP requests to the API server
import logging

# Configure logging for the bot
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables from .env file
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
API_SERVER_URL = os.getenv('API_SERVER_URL') # e.g., "https://your-render-api-service-name.onrender.com"
DISCORD_BOT_API_KEY = os.getenv('DISCORD_BOT_API_KEY') # This key should match the one in api_server.py

# Basic validation for essential environment variables
if not TOKEN:
    logging.error("DISCORD_TOKEN not found in .env file.")
    raise ValueError("DISCORD_TOKEN environment variable is not set. Please create a .env file or set the variable.")
if not API_SERVER_URL:
    logging.error("API_SERVER_URL not found in .env file.")
    raise ValueError("API_SERVER_URL environment variable is not set. Please set it to your Flask server URL (e.g., https://your-render-service.onrender.com).")
if not DISCORD_BOT_API_KEY:
    logging.error("DISCORD_BOT_API_KEY not found in .env file.")
    raise ValueError("DISCORD_BOT_API_KEY environment variable is not set. This key must match the one in your api_server.py.")


# --- Bot Setup ---
class DuelBot(commands.Bot):
    """
    Custom Discord bot class for duel tracing and triggering.
    It interacts with an external API server for all logic.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def setup_hook(self):
        """
        Syncs slash commands when the bot is ready.
        """
        logging.info("Attempting to sync slash commands...")
        await self.tree.sync()
        logging.info("Slash commands synced.")

    async def on_ready(self):
        """
        Event that runs when the bot is successfully logged in and ready.
        """
        logging.info(f'Logged in as {self.user} (ID: {self.user.id})')
        logging.info('------')


# Instantiate the bot
intents = discord.Intents.default()
bot = DuelBot(command_prefix='!', intents=intents) # '!' as a fallback prefix, focusing on slash commands

# --- Slash Command: /trace_duel ---
@bot.tree.command(name="trace_duel", description="Request to trace a duel between two Roblox players (will only work if spectating).")
@app_commands.describe(
    player1_id="The Roblox User ID of the first player (integer).",
    player2_id="The Roblox User ID of the second player (integer)."
)
async def trace_duel(interaction: discord.Interaction, player1_id: int, player2_id: int):
    """
    Handles the /trace_duel slash command. Sends a request to the API server
    to initiate a duel trace for the specified players.
    """
    await interaction.response.defer(ephemeral=False) # Acknowledge the interaction immediately

    if player1_id == player2_id:
        await interaction.followup.send("Please provide two different Roblox User IDs for the duel.", ephemeral=True)
        return

    try:
        payload = {
            "player1Id": player1_id,
            "player2Id": player2_id,
            "api_key": DISCORD_BOT_API_KEY, # Authenticate request to API server
            "requester_discord_id": str(interaction.user.id) # Track who requested the trace
        }
        
        response = requests.post(f"{API_SERVER_URL}/request_trace", json=payload)
        response.raise_for_status() 

        data = response.json()
        if data.get("success"):
            await interaction.followup.send(f"✅ Successfully requested to trace duel between Roblox IDs `{player1_id}` and `{player2_id}`.\n"
                                            "The Roblox client will now attempt to spectate this duel. "
                                            "Updates will be posted to the configured webhook channel.")
            logging.info(f"Trace request successful for {player1_id} and {player2_id}.")
        else:
            await interaction.followup.send(f"❌ Failed to request duel trace: {data.get('message', 'Unknown error from server.')}")
            logging.warning(f"Trace request failed for {player1_id} and {player2_id}: {data.get('message', 'Unknown error')}")

    except requests.exceptions.HTTPError as http_err:
        error_message = f"HTTP error occurred: {http_err}"
        if http_err.response.status_code == 409: # Conflict - another trace is active
            try:
                server_response = http_err.response.json()
                error_message = server_response.get('message', 'Another trace is already active.')
            except requests.exceptions.JSONDecodeError:
                pass
        await interaction.followup.send(f"An API server error occurred: `{error_message}`. Please check the server logs.")
        logging.error(f"HTTP Error requesting trace: {http_err} - Response: {http_err.response.text}")
    except requests.exceptions.ConnectionError as conn_err:
        logging.error(f"Error connecting to API server: {conn_err}")
        await interaction.followup.send(f"Unable to connect to the trace server at `{API_SERVER_URL}`. "
                                        "Please ensure the server is running and the URL is correct.")
    except requests.exceptions.Timeout as timeout_err:
        logging.error(f"Timeout connecting to API server: {timeout_err}")
        await interaction.followup.send(f"Request to the trace server timed out. Please try again.")
    except requests.exceptions.RequestException as req_err:
        logging.error(f"An unhandled request error occurred: {req_err}")
        await interaction.followup.send(f"An unexpected error occurred while communicating with the trace server: `{req_err}`")
    except Exception as e:
        logging.error(f"An unexpected error occurred in trace_duel command: {e}", exc_info=True)
        await interaction.followup.send(f"An unexpected internal error occurred. Please contact bot developer. Error: `{e}`")

# --- Slash Command: /trigger ---
@bot.tree.command(name="trigger", description="Tests the link by having the Roblox client print 'hello' in its console.")
async def trigger_test(interaction: discord.Interaction):
    """
    Handles the /trigger slash command. Sends a request to the API server
    to set a flag that the Roblox client will detect and act upon.
    """
    await interaction.response.defer(ephemeral=False)

    try:
        payload = {
            "api_key": DISCORD_BOT_API_KEY # Authenticate request
        }
        response = requests.post(f"{API_SERVER_URL}/set_trigger", json=payload)
        response.raise_for_status()

        data = response.json()
        if data.get("success"):
            await interaction.followup.send("✅ Trigger signal sent to Roblox client. Check its console for 'hello'.")
            logging.info("Trigger signal sent successfully.")
        else:
            await interaction.followup.send(f"❌ Failed to send trigger signal: {data.get('message', 'Unknown error from server.')}")
            logging.warning(f"Trigger signal failed: {data.get('message', 'Unknown error')}")

    except requests.exceptions.RequestException as e:
        logging.error(f"Error sending trigger to API server: {e}", exc_info=True)
        await interaction.followup.send(f"An error occurred while communicating with the API server for the trigger: `{e}`")
    except Exception as e:
        logging.error(f"An unexpected error occurred in trigger command: {e}", exc_info=True)
        await interaction.followup.send(f"An unexpected internal error occurred. Error: `{e}`")


# --- Main execution ---
if __name__ == "__main__":
    bot.run(TOKEN)
