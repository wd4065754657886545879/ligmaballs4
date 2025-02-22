import discord
import random
import string
import aiosqlite
import asyncio
from flask import Flask
from threading import Thread

# ----- Flask Web Server for Uptime (Optional) -----
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run_web)
    t.start()

# ----- Discord Bot Setup -----
intents = discord.Intents.default()
intents.members = True

DB_FILE = "keys.db"

async def init_db():
    """
    Initialize the SQLite database and create the keys table if it doesn't exist.
    If the table exists but is missing the 'temp_duration' column, add it.
    """
    db = await aiosqlite.connect(DB_FILE)
    # Create table without temp_duration (if it doesn't exist)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS keys (
            key TEXT PRIMARY KEY,
            used INTEGER,
            redeemed_by TEXT
        )
    """)
    # Check if temp_duration column exists; if not, add it.
    async with db.execute("PRAGMA table_info(keys)") as cursor:
        columns = await cursor.fetchall()
    if not any(col[1] == "temp_duration" for col in columns):
        await db.execute("ALTER TABLE keys ADD COLUMN temp_duration INTEGER DEFAULT 0")
    await db.commit()
    return db

def generate_key(length=17):
    """Generate a random 17-character alphanumeric string."""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)
        self.db = None
    
    async def setup_hook(self):
        # Initialize database connection.
        self.db = await init_db()
        # Sync slash commands globally.
        await self.tree.sync()
        print("Slash commands synced globally!")

client = MyClient()

# ---------------------- Permanent Key Command ----------------------

@client.tree.command(name="gen_key", description="Generate a permanent key (Owner-only command)")
async def gen_key(interaction: discord.Interaction):
    if not any(role.name == "OWNER" for role in interaction.user.roles):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    key = generate_key(17)
    try:
        # Insert key with temp_duration = 0 for a permanent key.
        await client.db.execute(
            "INSERT INTO keys (key, used, redeemed_by, temp_duration) VALUES (?, 0, NULL, 0)",
            (key,)
        )
        await client.db.commit()
    except Exception as e:
        print("Error inserting permanent key:", e)
        await interaction.response.send_message("Error generating key. Please try again.", ephemeral=True)
        return

    await interaction.response.send_message(f"Generated key: `{key}`", ephemeral=True)

# ---------------------- Redeem Command ----------------------

@client.tree.command(name="redeem", description="Redeem a key to receive the buyer role")
async def redeem(interaction: discord.Interaction, key: str):
    # Check if the key exists and fetch its usage and temporary duration.
    async with client.db.execute("SELECT used, temp_duration FROM keys WHERE key = ?", (key,)) as cursor:
        row = await cursor.fetchone()
    
    if row is None:
        await interaction.response.send_message("Invalid key.", ephemeral=True)
        return
    if row[0] != 0:
        await interaction.response.send_message("Invalid or already used key.", ephemeral=True)
        return

    temp_duration = row[1]  # 0 means permanent; > 0 means temporary.
    guild = interaction.guild

    # Find or create the "buyer" role.
    buyer_role = discord.utils.get(guild.roles, name="buyer")
    if buyer_role is None:
        buyer_role = await guild.create_role(name="buyer", colour=discord.Colour.gold())
    
    await interaction.user.add_roles(buyer_role)

    # Mark the key as used and record the redeemer.
    await client.db.execute(
        "UPDATE keys SET used = 1, redeemed_by = ? WHERE key = ?",
        (str(interaction.user.id), key)
    )
    await client.db.commit()

    # DM the redeemer with an embed.
    if temp_duration > 0:
        dm_embed = discord.Embed(
            title="🔒 Temporary Key Redeemed",
            description=(
                f"Hey! This is your temporary lifetime code.\n"
                f"You have been granted BUYER access for {temp_duration} minutes.\n\n"
                "I'm here to make sure you don't lose your key:\n\n"
                f"```\n{key}\n```"
            ),
            color=discord.Color.gold()
        )
    else:
        dm_embed = discord.Embed(
            title="🔒 Key Redeemed",
            description=(
                "Hey! This is your lifetime code.\n"
                "I'm here to make sure you don't lose your key:\n\n"
                f"```\n{key}\n```"
            ),
            color=discord.Color.gold()
        )
    try:
        await interaction.user.send(embed=dm_embed)
    except Exception as e:
        print("Error sending DM to redeemer:", e)

    # DM all owners with a notification embed.
    owner_role = discord.utils.get(guild.roles, name="OWNER")
    if owner_role is not None:
        if temp_duration > 0:
            owner_embed = discord.Embed(
                title="Temporary Key Redeemed Notification",
                description=f"{interaction.user.mention} has redeemed a temporary key: `{key}` (Duration: {temp_duration} minutes)",
                color=discord.Color.gold()
            )
        else:
            owner_embed = discord.Embed(
                title="Key Redeemed Notification",
                description=f"{interaction.user.mention} has redeemed key: `{key}`",
                color=discord.Color.gold()
            )
        for owner in owner_role.members:
            try:
                await owner.send(embed=owner_embed)
            except Exception as e:
                print("Error sending DM to owner:", e)

    # For temporary keys, schedule role removal after the specified time.
    if temp_duration > 0:
        async def remove_role_after_delay(user: discord.Member, delay: int):
            await asyncio.sleep(delay * 60)  # delay in seconds.
            try:
                await user.remove_roles(buyer_role)
                await user.send("Hey you lost your buyer. Your time ran out. Buy more time or get the real thing.")
            except Exception as e:
                print("Error removing buyer role after delay:", e)
        asyncio.create_task(remove_role_after_delay(interaction.user, temp_duration))

    await interaction.response.send_message("Thank you for Buying", ephemeral=True)

# ---------------------- Temporary Key Command ----------------------

@client.tree.command(name="time_key", description="Generate a temporary key for limited buyer access")
async def time_key(interaction: discord.Interaction, minutes: int):
    if not any(role.name == "OWNER" for role in interaction.user.roles):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    key = generate_key(17)
    try:
        # Insert the key with the specified temporary duration.
        await client.db.execute(
            "INSERT INTO keys (key, used, redeemed_by, temp_duration) VALUES (?, 0, NULL, ?)",
            (key, minutes)
        )
        await client.db.commit()
    except Exception as e:
        print("Error inserting temporary key:", e)
        await interaction.response.send_message("Error generating temporary key. Please try again.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"Temporary key generated: `{key}`\nIt is valid for {minutes} minutes once redeemed.",
        ephemeral=True
    )

# ----- Start the Web Server (Optional) and Run the Bot -----
keep_alive()  # For Replit "always on" trick.
client.run("YOUR_BOT_TOKEN")
