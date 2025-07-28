# app.py - Discord Broadcast Bot Backend
from flask import Flask, request, jsonify
import discord
import asyncio
import time
import threading

app = Flask(__name__)

# Dictionary to store active bot clients and their cooldowns
# In a real application, you might want a more persistent storage for tokens
# and manage multiple bots more robustly.
active_bots = {}
# Cooldowns per user/channel for broadcasting
# Format: {bot_token: {recipient_id: last_sent_timestamp}}
broadcast_cooldowns = {}
COOLDOWN_SECONDS = 1  # 1 second cooldown between messages to each recipient

# --- Helper functions for Discord Bot ---

class DiscordBotClient(discord.Client):
    """
    A custom Discord client to handle bot operations.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_ready = asyncio.Event() # Event to signal when bot is ready
        self.token = None # Store the token for later use

    async def on_ready(self):
        """
        Called when the bot successfully connects to Discord.
        """
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        self.is_ready.set() # Signal that the bot is ready

    async def on_connect(self):
        """
        Called when the bot connects to Discord.
        """
        print(f'Bot connected to Discord.')
        self.is_ready.clear() # Clear the ready state on reconnect/connect

    async def on_disconnect(self):
        """
        Called when the bot disconnects from Discord.
        """
        print(f'Bot disconnected from Discord.')
        self.is_ready.clear() # Clear the ready state on disconnect

    async def send_broadcast_message(self, recipient_id: int, message_content: str):
        """
        Sends a message to a specific user or channel.
        """
        try:
            # Try to get a user first
            user = self.get_user(recipient_id)
            if user:
                await user.send(message_content)
                print(f"Sent DM to user {user.name} ({recipient_id}): {message_content}")
                return True
            
            # If not a user, try to get a channel
            channel = self.get_channel(recipient_id)
            if channel:
                if isinstance(channel, discord.TextChannel) or isinstance(channel, discord.DMChannel):
                    await channel.send(message_content)
                    print(f"Sent message to channel {channel.name} ({recipient_id}): {message_content}")
                    return True
                else:
                    print(f"Recipient {recipient_id} is a voice or other unsupported channel type.")
                    return False
            else:
                print(f"Could not find user or channel with ID: {recipient_id}")
                return False
        except discord.errors.Forbidden:
            print(f"Bot does not have permission to send messages to {recipient_id}.")
            return False
        except Exception as e:
            print(f"Error sending message to {recipient_id}: {e}")
            return False

# --- Flask Endpoints ---

@app.route('/')
def index():
    """
    Simple root endpoint, for testing if the server is running.
    """
    return "Discord Broadcast Backend is running!"

@app.route('/api/check_bot_status', methods=['POST'])
async def check_bot_status():
    """
    Endpoint to check the status of a Discord bot using its token.
    Attempts to log in the bot if not already active.
    """
    data = request.json
    bot_token = data.get('botToken')

    if not bot_token:
        return jsonify({"status": "error", "message": "Bot token is required."}), 400

    # Check if bot is already active and ready
    if bot_token in active_bots and active_bots[bot_token].is_ready.is_set():
        bot_client = active_bots[bot_token]
        return jsonify({
            "status": "success",
            "message": "Bot is already logged in.",
            "username": bot_client.user.name,
            "discriminator": bot_client.user.discriminator,
            "id": str(bot_client.user.id)
        }), 200

    # If not active or not ready, try to log in
    try:
        # Create a new bot client instance
        intents = discord.Intents.default()
        intents.message_content = True # Required for message content
        intents.members = True # Required to get members for broadcasting
        intents.presences = False # Not strictly needed for this bot, can be False for performance
        
        bot_client = DiscordBotClient(intents=intents)
        bot_client.token = bot_token # Store token for this client instance

        # Use a separate thread to run the bot, as run() is blocking
        bot_thread = threading.Thread(target=lambda: asyncio.run(bot_client.start(bot_token)))
        bot_thread.daemon = True # Allow the thread to exit when the main program exits
        bot_thread.start()

        # Wait for the bot to become ready (timeout after 10 seconds)
        try:
            await asyncio.wait_for(bot_client.is_ready.wait(), timeout=10.0)
            active_bots[bot_token] = bot_client
            return jsonify({
                "status": "success",
                "message": "Bot logged in successfully.",
                "username": bot_client.user.name,
                "discriminator": bot_client.user.discriminator,
                "id": str(bot_client.user.id)
            }), 200
        except asyncio.TimeoutError:
            print(f"Bot with token {bot_token[:5]}... timed out during login.")
            # If timeout, stop the bot client and remove it
            if bot_client.is_ready.is_set(): # Check if it became ready just before timeout
                active_bots[bot_token] = bot_client
                return jsonify({
                    "status": "success",
                    "message": "Bot logged in successfully (after timeout wait).",
                    "username": bot_client.user.name,
                    "discriminator": bot_client.user.discriminator,
                    "id": str(bot_client.user.id)
                }), 200
            else:
                # Attempt to close the client gracefully if it didn't become ready
                try:
                    await bot_client.close()
                except Exception as e:
                    print(f"Error closing timed out bot client: {e}")
                return jsonify({"status": "error", "message": "فشل تسجيل دخول البوت: مهلة."}), 500

    except discord.errors.LoginFailure:
        print(f"Invalid token provided: {bot_token[:5]}...")
        return jsonify({"status": "error", "message": "فشل تسجيل دخول البوت: رمز غير صالح."}), 401
    except Exception as e:
        print(f"An unexpected error occurred during bot login: {e}")
        return jsonify({"status": "error", "message": f"حدث خطأ غير متوقع: {str(e)}"}), 500

@app.route('/api/broadcast', methods=['POST'])
async def broadcast_message():
    """
    Endpoint to broadcast a message to all users in guilds the bot is in,
    with a cooldown per user.
    """
    data = request.json
    bot_token = data.get('botToken')
    message_content = data.get('message')

    if not bot_token or not message_content:
        return jsonify({"status": "error", "message": "Bot token and message are required."}), 400

    bot_client = active_bots.get(bot_token)

    if not bot_client or not bot_client.is_ready.is_set():
        return jsonify({"status": "error", "message": "البوت غير متصل أو غير جاهز. يرجى التحقق من حالته أولاً."}), 400

    sent_count = 0
    failed_count = 0
    skipped_cooldown_count = 0
    
    # Initialize cooldowns for this bot if not present
    if bot_token not in broadcast_cooldowns:
        broadcast_cooldowns[bot_token] = {}

    # Gather all unique user IDs from all guilds the bot is in
    # Note: This requires the 'members' intent and for the bot to have 'Server Members Intent' enabled
    # in the Discord Developer Portal for your bot.
    unique_user_ids = set()
    for guild in bot_client.guilds:
        # Ensure 'members' intent is enabled and bot has permissions to see members
        if bot_client.intents.members:
            # Fetch members if not already cached (might be slow for large guilds)
            # await guild.chunk() # Uncomment if you need to ensure all members are cached
            for member in guild.members:
                if not member.bot: # Don't send messages to other bots
                    unique_user_ids.add(member.id)
        else:
            print(f"Warning: 'members' intent is not enabled for guild {guild.name}. Cannot fetch members.")
            # If members intent is not enabled, you might only be able to DM users who have
            # recently interacted with the bot or are in the bot's cache.
            # For a broadcast, it's crucial to have the members intent.

    # Also consider direct message channels if the bot has interacted with users directly
    for dm_channel in bot_client.private_channels:
        if isinstance(dm_channel, discord.DMChannel) and not dm_channel.recipient.bot:
            unique_user_ids.add(dm_channel.recipient.id)

    if not unique_user_ids:
        return jsonify({"status": "error", "message": "لم يتم العثور على مستخدمين للبث إليهم. تأكد من أن البوت في خوادم ولديه صلاحيات 'أعضاء الخادم'."}), 404

    current_time = time.time()
    
    # Send messages with cooldown
    for user_id in list(unique_user_ids): # Iterate over a copy to avoid issues if set changes
        last_sent_time = broadcast_cooldowns[bot_token].get(user_id, 0)
        
        if (current_time - last_sent_time) >= COOLDOWN_SECONDS:
            success = await bot_client.send_broadcast_message(user_id, message_content)
            if success:
                sent_count += 1
                broadcast_cooldowns[bot_token][user_id] = current_time
            else:
                failed_count += 1
            await asyncio.sleep(0.1) # Small delay to avoid hitting Discord rate limits too quickly
        else:
            skipped_cooldown_count += 1
            print(f"Skipping user {user_id} due to cooldown.")

    return jsonify({
        "status": "success",
        "message": f"تم محاولة بث الرسالة إلى {len(unique_user_ids)} مستخدمين. تم الإرسال إلى {sent_count}، فشل {failed_count}، تم تخطي {skipped_cooldown_count} بسبب فترة التهدئة.",
        "sent_count": sent_count,
        "failed_count": failed_count,
        "skipped_cooldown_count": skipped_cooldown_count
    }), 200

if __name__ == '__main__':
    # Run Flask app
    # Use a separate thread for the Flask app to not block the main thread
    # where the Discord bot's event loop will run.
    # In a production environment, use a WSGI server like Gunicorn.
    print("Starting Flask server...")
    app.run(debug=True, port=5000, use_reloader=False) # use_reloader=False to prevent duplicate bot instances
