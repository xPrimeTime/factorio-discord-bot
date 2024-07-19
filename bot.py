"""
Factorio Discord Bot

This bot monitors and manages a Factorio server running in a Docker container.
It provides real-time status updates, server management commands, and log
viewing capabilities through Discord.

Disclaimer: This project does not contain any human written lines of code.
This was all made and debugged using mainly Claude 3.5 sonnet,gpt4o and a lot
of thoughtful prompting. This took about 3 days to make for a novice

Author: xPrimeTime
Date: 7/19/2024
Version: 1.3
License: MIT

For more information, see the README.md file in the project repository.
"""

import os
import discord
from discord.ext import commands, tasks
import docker
import asyncio
import logging
from typing import Optional
from discord.ui import Button, View
from datetime import datetime, timezone, timedelta
from mcrcon import MCRcon
import random

# Set up logging
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('discord_bot')

# Set up Docker client
def get_docker_client() -> docker.DockerClient:
    try:
        return docker.from_env()
    except docker.errors.DockerException as e:
        logger.error(f"Failed to initialize Docker client: {e}")
        raise SystemExit("Docker client initialization failed. Please check Docker installation.")

docker_client = get_docker_client()

# Get configuration from environment variables
def get_env_variable(var_name: str, default: Optional[str] = None) -> str:
    value = os.environ.get(var_name, default)
    if value is None:
        logger.error(f"{var_name} environment variable is not set.")
        raise SystemExit(f"{var_name} must be set in the environment.")
    return value

DISCORD_TOKEN = get_env_variable('DISCORD_TOKEN')
STATUS_CHANNEL_ID = int(get_env_variable('STATUS_CHANNEL_ID', '0'))
UPDATE_INTERVAL = int(get_env_variable('UPDATE_INTERVAL', '60'))  # Default to 60 seconds
IDLE_TIMEOUT = 60  # seconds to wait before resetting to Idle
FACTORIO_RCON_PASSWORD = get_env_variable('FACTORIO_RCON_PASSWORD')
FACTORIO_RCON_PORT = int(get_env_variable('FACTORIO_RCON_PORT', '27015'))
FACTORIO_HOST = get_env_variable('FACTORIO_HOST', 'localhost')

if STATUS_CHANNEL_ID == 0:
    logger.error("STATUS_CHANNEL_ID environment variable is not set or is invalid.")
    raise SystemExit("STATUS_CHANNEL_ID must be set in the environment.")

# Set up Discord bot with explicit intents
intents = discord.Intents.default()
intents.message_content = True

class MyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reconnect_attempts = 0

    async def on_error(self, event_method, *args, **kwargs):
        logger.error(f'An error occurred in {event_method}', exc_info=True)

    async def on_connect(self):
        logger.info(f'Connected to Discord!')
        self.reconnect_attempts = 0

    async def on_disconnect(self):
        logger.warning('Disconnected from Discord')

    async def on_resumed(self):
        logger.info('Resumed Discord session')

    async def start(self, *args, **kwargs):
        while True:
            try:
                await super().start(*args, **kwargs)
            except discord.errors.ConnectionClosed as e:
                if e.code == 1000:
                    logger.warning(f'Connection closed with code 1000. Attempting to reconnect...')
                    await self.handle_reconnect()
                else:
                    logger.error(f'Connection closed with code {e.code}. Attempting to reconnect...')
                    await self.handle_reconnect()
            except Exception as e:
                logger.error(f'An unexpected error occurred: {e}')
                await self.handle_reconnect()

    async def handle_reconnect(self):
        self.reconnect_attempts += 1
        backoff = min(300, (2 ** self.reconnect_attempts) + (random.randint(0, 1000) / 1000))
        logger.info(f'Attempting to reconnect in {backoff:.2f} seconds (Attempt {self.reconnect_attempts})')
        await asyncio.sleep(backoff)

bot = MyBot(command_prefix='!', intents=intents)

# Global variable to store the latest bot status
latest_bot_status = "Idle"

class FactorioView(View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Start", style=discord.ButtonStyle.green, row=0)
    async def start_button(self, interaction: discord.Interaction, button: Button):
        await manage_container(interaction, 'factorio', 'start')

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.red, row=0)
    async def stop_button(self, interaction: discord.Interaction, button: Button):
        await manage_container(interaction, 'factorio', 'stop')

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.blurple, row=0)
    async def restart_button(self, interaction: discord.Interaction, button: Button):
        await manage_container(interaction, 'factorio', 'restart')

    @discord.ui.button(label="Logs", style=discord.ButtonStyle.gray, row=1)
    async def logs_button(self, interaction: discord.Interaction, button: Button):
        await show_logs(interaction)

    @discord.ui.button(label="Refresh", emoji="♻️", style=discord.ButtonStyle.gray, custom_id="refresh", row=1)
    async def refresh_button(self, interaction: discord.Interaction, button: Button):
        await refresh_status(interaction)

class CloseView(View):
    def __init__(self, bot, message):
        super().__init__(timeout=45.0)  # Set timeout to 45 seconds
        self.bot = bot
        self.message = message

    async def on_timeout(self):
        await self.message.delete()
        logger.info("Log message automatically closed after 45 seconds.")
        await update_bot_status("Logs automatically closed after 45 seconds")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.red)
    async def close_button(self, interaction: discord.Interaction, button: Button):
        await self.message.delete()
        logger.info("Log message manually closed.")
        await update_bot_status("Logs manually closed")
        self.stop()

@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord!')
    await bot.change_presence(activity=discord.Game(name="Factorio Server Monitor"))
    await clear_channel(STATUS_CHANNEL_ID)
    await update_bot_status("Idle", reset_to_idle=False)
    update_status.start()

async def clear_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel:
        await channel.purge(limit=None)
        logger.info(f"Cleared messages in channel {channel_id}")
    else:
        logger.error(f"Could not find channel with ID {channel_id}")

def parse_uptime(uptime_str):
    try:
        # Truncate nanoseconds to microseconds for compatibility
        truncated_uptime_str = uptime_str[:-4] + 'Z'
        timestamp = datetime.strptime(truncated_uptime_str, '%Y-%m-%dT%H:%M:%S.%fZ')
    except ValueError as e:
        logger.error(f"Error parsing datetime: {e}")
        return "Unknown"

    # Get the current time in UTC
    current_time = datetime.utcnow()

    # Calculate the difference (uptime)
    uptime_duration = current_time - timestamp

    # Extract days, hours, minutes, and seconds
    days = uptime_duration.days
    hours, remainder = divmod(uptime_duration.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    # Build the readable uptime string
    uptime_str = []
    if days > 0:
        uptime_str.append(f"{days}d")
    if hours > 0:
        uptime_str.append(f"{hours}h")
    if minutes > 0:
        uptime_str.append(f"{minutes}m")
    if seconds > 0:
        uptime_str.append(f"{seconds}s")

    return ' '.join(uptime_str) or '0s'

async def get_player_count():
    try:
        with MCRcon(FACTORIO_HOST, FACTORIO_RCON_PASSWORD, port=FACTORIO_RCON_PORT) as mcr:
            response = mcr.command("/players online")
            # Split the response into lines and count non-empty lines after the first one
            lines = response.strip().split('\n')
            if len(lines) > 1:
                # Count non-empty lines after the first line (which is usually a header)
                player_count = sum(1 for line in lines[1:] if line.strip())
            else:
                player_count = 0
            return player_count
    except Exception as e:
        logger.error(f"Error getting player count: {e}")
        return None

async def get_factorio_stats():
    try:
        container = docker_client.containers.get('factorio')
        container.reload()  # Ensure we have the latest state
        status = container.status
        
        if status == 'running':
            stats = container.stats(stream=False)
            cpu_usage = stats['cpu_stats']['cpu_usage']['total_usage'] / stats['cpu_stats']['system_cpu_usage'] * 100
            
            # Calculate RAM usage in MiB and total RAM in GiB
            ram_usage = stats['memory_stats']['usage'] / (1024 * 1024)  # Convert to MiB
            ram_limit = stats['memory_stats']['limit'] / (1024 * 1024 * 1024)  # Convert to GiB
            
            uptime = parse_uptime(container.attrs['State']['StartedAt'])
            player_count = await get_player_count()
        else:
            cpu_usage = 'N/A'
            ram_usage = 'N/A'
            ram_limit = 'N/A'
            uptime = 'N/A'
            player_count = 'N/A'
        
        return {
            'status': status,
            'cpu_usage': f"{cpu_usage:.2f}%" if isinstance(cpu_usage, float) else cpu_usage,
            'ram_usage': f"{ram_usage:.0f}MiB" if isinstance(ram_usage, float) else ram_usage,
            'ram_limit': f"{ram_limit:.2f}GiB" if isinstance(ram_limit, float) else ram_limit,
            'uptime': uptime,
            'player_count': player_count
        }
    except docker.errors.NotFound:
        logger.info("Factorio container not found. It might be stopped.")
        return {
            'status': 'stopped',
            'cpu_usage': 'N/A',
            'ram_usage': 'N/A',
            'ram_limit': 'N/A',
            'uptime': 'N/A',
            'player_count': 'N/A'
        }
    except Exception as e:
        logger.error(f"Error getting Factorio stats: {e}")
        return None

async def send_factorio_status(channel):
    global latest_bot_status
    stats = await get_factorio_stats()
    if not stats:
        logger.error("Failed to get Factorio server stats.")
        return

    embed = discord.Embed(title="Factorio Server Status", color=discord.Color.orange())
    
    # Add emoji based on server status
    status_emoji = "🟢" if stats['status'] == 'running' else "🔴"
    embed.add_field(name="Server Status", value=f"{status_emoji} {stats['status']}", inline=False)
    
    embed.add_field(name="CPU Usage", value=stats['cpu_usage'], inline=True)
    embed.add_field(name="RAM Usage", value=f"{stats['ram_usage']} / {stats['ram_limit']}", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)  # Empty field for alignment
    
    embed.add_field(name="Uptime", value=stats['uptime'], inline=True)
    embed.add_field(name="Players", value=stats['player_count'], inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)  # Empty field for alignment
    
    embed.add_field(name="Bot Status", value=latest_bot_status, inline=False)
    embed.set_footer(text=f"Last updated: {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")

    view = FactorioView(bot)
    
    # Try to find an existing status message
    async for message in channel.history(limit=50):
        if message.author == bot.user and message.embeds and message.embeds[0].title == "Factorio Server Status":
            await message.edit(embed=embed, view=view)
            return

    # If no existing message found, send a new one
    await channel.send(embed=embed, view=view)

async def update_bot_status(status: str, reset_to_idle: bool = True):
    global latest_bot_status
    latest_bot_status = status
    await bot.change_presence(activity=discord.Game(name=status))
    channel = bot.get_channel(STATUS_CHANNEL_ID)
    if channel:
        await send_factorio_status(channel)
    
    if reset_to_idle:
        # Schedule the status to be reset to Idle after IDLE_TIMEOUT seconds
        bot.loop.create_task(reset_status_to_idle())

async def reset_status_to_idle():
    await asyncio.sleep(IDLE_TIMEOUT)
    global latest_bot_status
    if latest_bot_status != "Idle":
        await update_bot_status("Idle", reset_to_idle=False)

async def manage_container(interaction: discord.Interaction, container_name: str, action: str):
    try:
        await interaction.response.defer(ephemeral=True)
        
        container = docker_client.containers.get(container_name)
        
        await update_bot_status(f"Factorio server {action}ing...", reset_to_idle=False)
        
        getattr(container, action)()
        
        logger.info(f"{action.capitalize()}ing Factorio server...")
        
        max_attempts = 12
        for attempt in range(max_attempts):
            await asyncio.sleep(5)
            container.reload()
            if action == 'stop' and container.status == 'exited':
                await update_bot_status("Factorio server stopped")
                break
            elif action in ['start', 'restart'] and container.status == 'running':
                await update_bot_status(f"Factorio server {action}ed")
                break
        else:
            await update_bot_status(f"Factorio server {action} command issued, but status is {container.status}")
        
    except docker.errors.NotFound:
        await update_bot_status(f"Error: Factorio container not found")
    except Exception as e:
        logger.error(f"Error managing Factorio container: {e}")
        await update_bot_status(f"Error: {action} failed")

async def show_logs(interaction: discord.Interaction, lines: int = 20):
    try:
        container = docker_client.containers.get('factorio')
        logs = container.logs(tail=lines).decode('utf-8')
        
        embed = discord.Embed(title="Factorio Server Logs", description=f"```\n{logs}\n```", color=discord.Color.gold())
        
        # Defer the response to avoid interaction timeout
        await interaction.response.defer()
        
        # Send the message and get the message object
        message = await interaction.followup.send(embed=embed, wait=True)
        
        # Create the view with the message object
        view = CloseView(bot, message)
        
        # Edit the message to include the view
        await message.edit(view=view)
        
        logger.info("Logs have been sent to Discord.")
        await update_bot_status("Logs displayed")
        
        # Start the view to enable the timeout functionality
        await view.wait()
        
    except docker.errors.NotFound:
        logger.error("Factorio container not found.")
        logger.info("Attempted to fetch logs, but Factorio container was not found.")
        await update_bot_status("Error: Factorio container not found")
    except Exception as e:
        logger.error(f"Error fetching Factorio logs: {e}")
        logger.info("Attempted to fetch logs, but an error occurred.")
        await update_bot_status("Error: Failed to fetch logs")

async def refresh_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await update_bot_status("Status manually refreshed")
    logger.info("Status manually refreshed.")

@tasks.loop(seconds=UPDATE_INTERVAL)
async def update_status():
    channel = bot.get_channel(STATUS_CHANNEL_ID)
    if not channel:
        logger.error(f"Could not find channel with ID {STATUS_CHANNEL_ID}")
        return

    # Only update the status if it's currently "Idle"
    if latest_bot_status == "Idle":
        await send_factorio_status(channel)

if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        logger.error("Failed to login to Discord. Please check your token.")
    except Exception as e:
        logger.error(f"An unexpected error occurred while running the bot: {e}")
