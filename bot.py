"""PURIFY Lite - single-file Discord bot (moderation, security, tickets, leveling and more).

Railway: set DISCORD_TOKEN (+ OWNER_IDS), start command `python bot.py`.
Requires discord.py>=2.4 (see requirements.txt).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path
from time import monotonic

import discord
from discord import app_commands
from discord.ext import commands

try:  # optional: loads .env when running locally
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------- config
TOKEN = os.getenv("DISCORD_TOKEN", "")
PREFIX = os.getenv("BOT_PREFIX", ".")
DB_URL = os.getenv("DATABASE_URL", "sqlite:///data/purify.db")
OWNER_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("OWNER_IDS", ""))}
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("purify")

DURATION = re.compile(r"^(\d+)\s*([smhdw])$", re.I)
UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
INVITES = ("discord.gg/", "discord.com/invite/", "discordapp.com/invite/")
TICKET_CATEGORY = "PURIFY Tickets"
DEFAULT_WELCOME = "Welcome {user} to **{server}**! You are member #{count} 🎉"
DEFAULT_GREET = "Welcome {user} to {server}. We are glad to have you here."
BLURPLE = discord.Color.blurple()


def parse_duration(text: str) -> timedelta | None:
    m = DURATION.fullmatch(text.strip())
    return timedelta(**{UNITS[m.group(2).lower()]: int(m.group(1))}) if m else None


async def say(i: discord.Interaction, text: str = None, **kw) -> None:
    """Ephemeral reply that works whether or not the interaction was already answered."""
    if i.response.is_done():
        await i.followup.send(text, ephemeral=True, **kw)
    else:
        await i.response.send_message(text, ephemeral=True, **kw)


async def out(ctx: commands.Context, text: str = None, **kw) -> None:
    """Reply helper for hybrid commands (ephemeral on slash, normal reply on prefix)."""
    await ctx.reply(text, ephemeral=True, mention_author=False, **kw)


def admin_only():
    return app_commands.checks.has_permissions(administrator=True)


# ---------------------------------------------------------------- database
class Database:
    def __init__(self, url: str, default_prefix: str = "."):
        self.default_prefix = default_prefix
        self._cache: dict[int, dict] = {}
        path = url.removeprefix("sqlite:///")  # sqlite:///data/x.db -> data/x.db, sqlite:////abs/x.db -> /abs/x.db
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS guilds (id INTEGER PRIMARY KEY, prefix TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS warnings (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER,
                moderator_id INTEGER, reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS xp (guild_id INTEGER, user_id INTEGER, amount INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, user_id));
        """)
        self.conn.commit()

    # ---- guild settings (cached) ----
    def guild(self, guild_id: int) -> dict:
        if guild_id not in self._cache:
            row = self.conn.execute("SELECT prefix, settings FROM guilds WHERE id=?", (guild_id,)).fetchone()
            self._cache[guild_id] = (
                {"prefix": row["prefix"], "settings": json.loads(row["settings"] or "{}")}
                if row else {"prefix": self.default_prefix, "settings": {}}
            )
        return self._cache[guild_id]

    def save_guild(self, guild_id: int, prefix: str | None = None, values: dict | None = None) -> None:
        cur = self.guild(guild_id)
        new = {"prefix": prefix or cur["prefix"], "settings": {**cur["settings"], **(values or {})}}
        self.conn.execute(
            "INSERT INTO guilds(id,prefix,settings) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET prefix=excluded.prefix, settings=excluded.settings",
            (guild_id, new["prefix"], json.dumps(new["settings"], separators=(",", ":"))),
        )
        self.conn.commit()
        self._cache[guild_id] = new

    def get(self, guild_id: int, key: str, default=None):
        return self.guild(guild_id)["settings"].get(key, default)

    def set(self, guild_id: int, key: str, value) -> None:
        self.save_guild(guild_id, values={key: value})

    # ---- warnings ----
    def warn(self, guild_id: int, user_id: int, moderator_id: int, reason: str) -> None:
        self.conn.execute("INSERT INTO warnings(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
                          (guild_id, user_id, moderator_id, reason))
        self.conn.commit()

    def warnings(self, guild_id: int, user_id: int):
        return self.conn.execute("SELECT * FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
                                 (guild_id, user_id)).fetchall()

    # ---- xp ----
    def xp(self, guild_id: int, user_id: int) -> tuple[int, int]:
        row = self.conn.execute("SELECT amount, level FROM xp WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
        return (row["amount"], row["level"]) if row else (0, 0)

    def add_xp(self, guild_id: int, user_id: int, amount: int) -> tuple[int, int]:
        total = max(0, self.xp(guild_id, user_id)[0] + amount)
        level = total // 100
        self.conn.execute(
            "INSERT INTO xp(guild_id,user_id,amount,level) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET amount=excluded.amount, level=excluded.level",
            (guild_id, user_id, total, level),
        )
        self.conn.commit()
        return total, level

    def top_xp(self, guild_id: int, limit: int = 10):
        return self.conn.execute("SELECT user_id, amount, level FROM xp WHERE guild_id=? ORDER BY amount DESC LIMIT ?",
                                 (guild_id, limit)).fetchall()


# ---------------------------------------------------------------- commands
class Core(commands.Cog):
    security = app_commands.Group(name="security", description="Server security", guild_only=True)
    ticket = app_commands.Group(name="ticket", description="Support tickets", guild_only=True)
    xp = app_commands.Group(name="xp", description="XP management", guild_only=True)
    logging = app_commands.Group(name="logging", description="Log channels", guild_only=True)
    owner = app_commands.Group(name="owner", description="Owner tools")

    def __init__(self, bot: "PurifyBot"):
        self.bot = bot
        self.db = bot.db
        self.bursts = defaultdict(lambda: deque(maxlen=12))
        self.joins = defaultdict(lambda: deque(maxlen=20))
        self.xp_seen: dict[tuple[int, int], float] = {}
        self.voice_locks = defaultdict(asyncio.Lock)

    # ---- helpers
    async def send_log(self, guild: discord.Guild, text: str, category: str = "security"):
        channel = guild.get_channel(self.db.get(guild.id, f"log_{category}") or 0)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(text)
            except discord.HTTPException:
                pass

    async def can_act(self, i: discord.Interaction, member: discord.Member) -> bool:
        g = i.guild
        blocked = (
            member == g.owner
            or member.top_role >= g.me.top_role
            or (i.user != g.owner and member.top_role >= i.user.top_role)
        )
        if blocked:
            await say(i, "❌ Role hierarchy: I (or you) can't act on that member.")
        return not blocked

    def config_embed(self, guild_id: int) -> discord.Embed:
        g = self.db.guild(guild_id)
        s = g["settings"]
        e = discord.Embed(title="PURIFY Configuration", color=BLURPLE)
        e.add_field(name="Prefix", value=f"`{g['prefix']}`")
        e.add_field(name="Anti-nuke", value=str(s.get("anti_nuke", "off")))
        e.add_field(name="Anti-raid", value="on" if s.get("anti_raid") else "off")
        e.add_field(name="Anti-link", value="on" if s.get("anti_link") else "off")
        e.add_field(name="Logging", value="on" if s.get("logging_enabled") else "off")
        e.add_field(name="Leveling", value="on" if s.get("leveling_enabled", True) else "off")
        return e

    # ---- events
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return
        if await self.guard(message):
            return
        gid, uid = message.guild.id, message.author.id
        if self.db.get(gid, "leveling_enabled", True) and monotonic() - self.xp_seen.get((gid, uid), 0) > 30:
            self.xp_seen[(gid, uid)] = monotonic()
            self.db.add_xp(gid, uid, 5)

    async def guard(self, m: discord.Message) -> bool:
        """Anti-link + anti-spam. Returns True if the message was actioned."""
        perms = m.author.guild_permissions
        if perms.manage_messages or perms.administrator:
            return False
        g = m.guild
        if self.db.get(g.id, "anti_link", False) and any(x in m.content.lower() for x in INVITES):
            try:
                await m.delete()
            except discord.HTTPException:
                pass
            await self.send_log(g, f"🚫 Removed an invite from {m.author.mention}.")
            return True
        now = monotonic()
        hits = self.bursts[(g.id, m.author.id)]
        hits.append(now)
        while hits and now - hits[0] > 8:
            hits.popleft()
        if len(hits) >= 7:
            hits.clear()
            try:
                await m.delete()
                await m.author.timeout(timedelta(minutes=2), reason="PURIFY anti-spam")
            except discord.HTTPException:
                pass
            await self.send_log(g, f"🚨 Anti-spam action applied to {m.author.mention}.")
            return True
        return False

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not member.bot:
            await self.welcome(member)
        await self.raid_watch(member)

    def fill(self, text: str, member: discord.Member, spoken: bool = False) -> str:
        who = member.display_name if spoken else member.mention
        return text.replace("{user}", who).replace("{server}", member.guild.name).replace("{count}", str(member.guild.member_count))

    async def welcome(self, member: discord.Member):
        """Welcome message + the greet-voice 'waiting' role for new members."""
        g, s = member.guild, self.db.guild(member.guild.id)["settings"]
        channel = g.get_channel(s.get("welcome_channel") or 0)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(self.fill(s.get("welcome_text") or DEFAULT_WELCOME, member))
            except discord.HTTPException:
                pass
        role = g.get_role(s.get("greet_role") or 0)
        if role:
            try:
                await member.add_roles(role, reason="PURIFY greet voice: waiting to be welcomed")
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Member with the greet role enters the greet voice channel -> spoken welcome, role removed."""
        if member.bot or after.channel is None or after.channel == before.channel:
            return
        s = self.db.guild(member.guild.id)["settings"]
        if after.channel.id != s.get("greet_voice"):
            return
        role = member.guild.get_role(s.get("greet_role") or 0)
        if role is None or role not in member.roles:
            return
        async with self.voice_locks[member.guild.id]:
            if await self.speak(after.channel, self.fill(s.get("greet_text") or DEFAULT_GREET, member, spoken=True)):
                try:
                    await member.remove_roles(role, reason="PURIFY greet voice: welcomed")
                except discord.HTTPException:
                    pass

    async def speak(self, channel: discord.VoiceChannel, text: str) -> bool:
        """Join the channel, say `text` with gTTS, leave. Needs gTTS + FFmpeg (see Dockerfile)."""
        try:
            from gtts import gTTS
        except ImportError:
            LOG.error("gTTS is not installed; greet voice disabled")
            return False
        path = os.path.join(tempfile.gettempdir(), f"greet_{channel.guild.id}.mp3")
        vc = None
        try:
            await asyncio.to_thread(lambda: gTTS(text=text, lang=os.getenv("GREET_LANG", "en")).save(path))
            vc = channel.guild.voice_client or await channel.connect(timeout=20)
            if vc.channel != channel:
                await vc.move_to(channel)
            done, loop = asyncio.Event(), asyncio.get_running_loop()
            vc.play(discord.FFmpegPCMAudio(path), after=lambda _e: loop.call_soon_threadsafe(done.set))
            await asyncio.wait_for(done.wait(), 60)
            return True
        except Exception:
            LOG.exception("Greet voice failed (check Connect/Speak permissions, FFmpeg, PyNaCl)")
            return False
        finally:
            if vc:
                await vc.disconnect(force=True)
            try:
                os.remove(path)
            except OSError:
                pass

    async def raid_watch(self, member: discord.Member):
        if not self.db.get(member.guild.id, "anti_raid", False):
            return
        now = monotonic()
        joins = self.joins[member.guild.id]
        joins.append(now)
        while joins and now - joins[0] > 20:
            joins.popleft()
        if len(joins) >= 8:
            await self.send_log(member.guild, f"⚠️ Join burst detected: {len(joins)} joins in 20 seconds.")
            joins.clear()

    # ---- utility (slash + prefix)
    @commands.hybrid_command(name="ping", description="Check PURIFY latency")
    async def ping(self, ctx: commands.Context):
        await ctx.reply(f"🏓 Pong! `{round(self.bot.latency * 1000)}ms`", ephemeral=True, mention_author=False)

    @commands.hybrid_command(name="help", description="Open the PURIFY command list")
    async def help(self, ctx: commands.Context):
        e = discord.Embed(title="🛡️ PURIFY Lite", description=f"Slash commands, or the prefix (`{PREFIX}`) for hybrid ones.", color=BLURPLE)
        e.add_field(name="Utility", value="`ping` `help` `serverinfo` `userinfo`", inline=False)
        e.add_field(name="Setup", value="`setup` `config` `dashboard` `prefix`", inline=False)
        e.add_field(name="Moderation", value="`ban` `unban` `kick` `softban` `timeout` `untimeout` `warn` `warns` `purge`\nPrefix tip: reply to a member's message with `{p}ban`, `{p}kick`, `{p}timeout 10m`…".format(p=PREFIX), inline=False)
        e.add_field(name="Security", value="`/security` → status · antinuke\n`antiraidsetup` `antilinksetup` `quarantine`", inline=False)
        e.add_field(name="Leveling", value="`levelsetup` `rank` `leaderboard` · `/xp` → add · remove", inline=False)
        e.add_field(name="Tickets", value="`/ticket` → setup · create · close · add · remove", inline=False)
        e.add_field(name="Logging", value="`loggingsetup` · `/logging` → set · securitylog · modlog\n`securitylog` `modlog`", inline=False)
        e.add_field(name="Notifications", value="`youtubenotifiersetup` `tiktoknotifiersetup` `instagramnotifiersetup`", inline=False)
        e.add_field(name="Welcome", value="`welcomesetup` `welcomeoff` `greetvoicesetup`", inline=False)
        e.add_field(name="Voice / Music", value="`voicemastersetup` `play` `pause` `resume` `queue`", inline=False)
        e.add_field(name="Owner", value="`/owner stats`", inline=False)
        await ctx.reply(embed=e, ephemeral=True, mention_author=False)

    @commands.hybrid_command(name="serverinfo", description="Show server information")
    @commands.guild_only()
    async def serverinfo(self, ctx: commands.Context):
        g = ctx.guild
        e = discord.Embed(title=g.name, color=BLURPLE)
        e.add_field(name="Members", value=str(g.member_count))
        e.add_field(name="Channels", value=str(len(g.channels)))
        e.add_field(name="Roles", value=str(len(g.roles)))
        await ctx.reply(embed=e, ephemeral=True, mention_author=False)

    @commands.hybrid_command(name="userinfo", description="Show member information")
    @commands.guild_only()
    async def userinfo(self, ctx: commands.Context, member: discord.Member | None = None):
        u = member or ctx.author
        e = discord.Embed(title=str(u), color=u.color)
        e.set_thumbnail(url=u.display_avatar.url)
        e.add_field(name="ID", value=str(u.id))
        e.add_field(name="Created", value=discord.utils.format_dt(u.created_at, "R"))
        await ctx.reply(embed=e, ephemeral=True, mention_author=False)

    # ---- setup (slash + prefix)
    @commands.hybrid_command(name="setup", description="Enable sensible PURIFY defaults")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def setup_defaults(self, ctx: commands.Context):
        self.db.save_guild(ctx.guild.id, values={"anti_nuke": "setup", "anti_raid": True, "anti_link": True,
                                                 "logging_enabled": True, "ticket_setup": True, "leveling_enabled": True})
        await ctx.reply("✅ PURIFY is configured with safe defaults. Use `/config` to review.", ephemeral=True, mention_author=False)

    @commands.hybrid_command(name="config", description="View PURIFY configuration")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def config(self, ctx: commands.Context):
        await ctx.reply(embed=self.config_embed(ctx.guild.id), ephemeral=True, mention_author=False)

    @commands.hybrid_command(name="dashboard", description="Open the admin dashboard")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def dashboard(self, ctx: commands.Context):
        await ctx.reply(embed=self.config_embed(ctx.guild.id), ephemeral=True, mention_author=False)

    @commands.hybrid_command(name="prefix", description="Change the prefix (1-5 characters)")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_prefix(self, ctx: commands.Context, prefix: str):
        if not 1 <= len(prefix) <= 5 or any(c.isspace() for c in prefix):
            return await ctx.reply("❌ Prefix must be 1-5 non-space characters.", ephemeral=True, mention_author=False)
        self.db.save_guild(ctx.guild.id, prefix=prefix)
        await ctx.reply(f"✅ Prefix set to `{prefix}`.", ephemeral=True, mention_author=False)

    # ---- moderation: slash OR prefix. With the prefix you can also just reply to the
    # target's message, e.g. reply ".ban spamming" / ".kick" / ".timeout 10m".
    async def reply_member(self, ctx: commands.Context) -> discord.Member | None:
        ref = ctx.message.reference if ctx.interaction is None else None
        if not ref:
            return None
        msg = ref.resolved if isinstance(ref.resolved, discord.Message) else None
        if msg is None and ref.message_id:
            try:
                msg = await ctx.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return None
        return ctx.guild.get_member(msg.author.id) if msg else None

    async def victim(self, ctx: commands.Context, user: discord.Member | None) -> discord.Member | None:
        user = user or await self.reply_member(ctx)
        if user is None:
            await out(ctx, f"❌ Mention a member, or reply to their message with `{ctx.clean_prefix}{ctx.command.name}`.")
            return None
        g = ctx.guild
        if user == g.owner or user.top_role >= g.me.top_role or (ctx.author != g.owner and user.top_role >= ctx.author.top_role):
            await out(ctx, "❌ Role hierarchy: I (or you) can't act on that member.")
            return None
        return user

    @commands.hybrid_command(name="ban", description="Ban a member (prefix: reply to their message)")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx: commands.Context, user: discord.Member | None = None, *, reason: str = "No reason provided"):
        m = await self.victim(ctx, user)
        if m:
            await m.ban(reason=reason)
            await out(ctx, f"✅ Banned {m.mention}.")
            await self.send_log(ctx.guild, f"🔨 Banned {m} by {ctx.author}: {reason}", "mod")

    @commands.hybrid_command(name="unban", description="Unban a user by ID")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    async def unban(self, ctx: commands.Context, user_id: str, *, reason: str = "No reason provided"):
        try:
            await ctx.guild.unban(discord.Object(int(user_id)), reason=reason)
        except (ValueError, discord.NotFound):
            return await out(ctx, "❌ That user ID isn't banned.")
        await out(ctx, f"✅ Unbanned `{user_id}`.")
        await self.send_log(ctx.guild, f"🔓 Unbanned {user_id} by {ctx.author}: {reason}", "mod")

    @commands.hybrid_command(name="kick", description="Kick a member (prefix: reply to their message)")
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    async def kick(self, ctx: commands.Context, user: discord.Member | None = None, *, reason: str = "No reason provided"):
        m = await self.victim(ctx, user)
        if m:
            await m.kick(reason=reason)
            await out(ctx, f"✅ Kicked {m.mention}.")
            await self.send_log(ctx.guild, f"👢 Kicked {m} by {ctx.author}: {reason}", "mod")

    @commands.hybrid_command(name="softban", description="Ban + unban to wipe a member's recent messages")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    async def softban(self, ctx: commands.Context, user: discord.Member | None = None, *, reason: str = "No reason provided"):
        m = await self.victim(ctx, user)
        if m:
            await m.ban(reason=f"Softban: {reason}", delete_message_seconds=86400)
            await ctx.guild.unban(m, reason="Softban complete")
            await out(ctx, f"✅ Softbanned {m.mention}.")
            await self.send_log(ctx.guild, f"🔨 Softbanned {m} by {ctx.author}: {reason}", "mod")

    @commands.hybrid_command(name="timeout", description="Timeout a member, e.g. 10m / 1h / 7d (prefix: reply to them)")
    @app_commands.describe(duration="10m, 1h or 7d (max 28d)")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def timeout(self, ctx: commands.Context, duration: str, user: discord.Member | None = None, *, reason: str = "No reason provided"):
        span = parse_duration(duration)
        if span is None or span > timedelta(days=28):
            return await out(ctx, f"❌ Usage: `{ctx.clean_prefix}timeout 10m [@user] [reason]` (or reply to them). Units: s m h d w, max 28d.")
        m = await self.victim(ctx, user)
        if m:
            await m.timeout(span, reason=reason)
            await out(ctx, f"✅ Timed out {m.mention} for `{duration}`.")
            await self.send_log(ctx.guild, f"⏳ Timed out {m} ({duration}) by {ctx.author}: {reason}", "mod")

    @commands.hybrid_command(name="untimeout", description="Remove a timeout (prefix: reply to them)")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def untimeout(self, ctx: commands.Context, user: discord.Member | None = None):
        m = await self.victim(ctx, user)
        if m:
            await m.timeout(None)
            await out(ctx, f"✅ Removed timeout from {m.mention}.")

    @commands.hybrid_command(name="warn", description="Warn a member (prefix: reply to their message)")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx: commands.Context, user: discord.Member | None = None, *, reason: str = "No reason provided"):
        m = await self.victim(ctx, user)
        if m:
            self.db.warn(ctx.guild.id, m.id, ctx.author.id, reason)
            await out(ctx, f"⚠️ Warned {m.mention}.")
            await self.send_log(ctx.guild, f"⚠️ Warned {m} by {ctx.author}: {reason}", "mod")

    @commands.hybrid_command(name="warns", description="List a member's warnings (prefix: reply to them)")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def warns(self, ctx: commands.Context, user: discord.Member | None = None):
        m = user or await self.reply_member(ctx)
        if m is None:
            return await out(ctx, "❌ Mention a member, or reply to their message.")
        rows = self.db.warnings(ctx.guild.id, m.id)
        e = discord.Embed(title=f"Warnings: {m}", color=discord.Color.orange())
        e.description = "\n".join(f"#{r['id']}: {r['reason']}" for r in rows) or "No warnings."
        await out(ctx, embed=e)

    @commands.hybrid_command(name="purge", description="Delete recent messages (1-200), optionally only one member's")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def purge(self, ctx: commands.Context, amount: commands.Range[int, 1, 200], user: discord.Member | None = None):
        user = user or await self.reply_member(ctx)
        await ctx.defer(ephemeral=True)
        deleted = await ctx.channel.purge(limit=amount, check=(lambda m: m.author == user) if user else None)
        extra = {"delete_after": 5} if ctx.interaction is None else {}
        await ctx.send(f"✅ Deleted {len(deleted)} messages.", ephemeral=True, **extra)

    # ---- security
    @security.command(name="status", description="Show security status")
    @admin_only()
    async def security_status(self, i: discord.Interaction):
        s = self.db.guild(i.guild.id)["settings"]
        await say(i, f"🛡️ Anti-nuke: `{s.get('anti_nuke', 'off')}` · Anti-raid: `{s.get('anti_raid', False)}` · "
                     f"Anti-link: `{s.get('anti_link', False)}` · Anti-spam: `on`")

    @security.command(name="antinuke", description="Set anti-nuke mode")
    @admin_only()
    async def antinuke(self, i: discord.Interaction, action: str):
        self.db.set(i.guild.id, "anti_nuke", action.lower())
        await say(i, f"✅ Anti-nuke set to `{action.lower()}`.")

    @app_commands.command(name="antiraidsetup", description="Enable anti-raid")
    @app_commands.guild_only()
    @admin_only()
    async def antiraidsetup(self, i: discord.Interaction):
        self.db.set(i.guild.id, "anti_raid", True)
        await say(i, "✅ Anti-raid enabled.")

    @app_commands.command(name="antilinksetup", description="Enable anti-link (Discord invites)")
    @app_commands.guild_only()
    @admin_only()
    async def antilinksetup(self, i: discord.Interaction):
        self.db.set(i.guild.id, "anti_link", True)
        await say(i, "✅ Anti-link enabled.")

    @app_commands.command(name="quarantine", description="Give a member the quarantine role")
    @app_commands.guild_only()
    @admin_only()
    async def quarantine(self, i: discord.Interaction, user: discord.Member):
        if not await self.can_act(i, user):
            return
        role = discord.utils.get(i.guild.roles, name="Purify Quarantine") or await i.guild.create_role(name="Purify Quarantine")
        await user.add_roles(role, reason="PURIFY quarantine")
        await say(i, f"✅ Quarantined {user.mention}.")
        await self.send_log(i.guild, f"🔒 Quarantined {user} by {i.user}.")

    # ---- leveling
    @app_commands.command(name="levelsetup", description="Enable leveling")
    @app_commands.guild_only()
    @admin_only()
    async def levelsetup(self, i: discord.Interaction):
        self.db.set(i.guild.id, "leveling_enabled", True)
        await say(i, "✅ Leveling enabled.")

    @commands.hybrid_command(name="rank", description="Show your (or someone's) level")
    @commands.guild_only()
    async def rank(self, ctx: commands.Context, user: discord.Member | None = None):
        t = user or ctx.author
        amount, level = self.db.xp(ctx.guild.id, t.id)
        await ctx.reply(f"📈 **{t.display_name}** — Level `{level}` · XP `{amount}`", ephemeral=True, mention_author=False)

    @commands.hybrid_command(name="leaderboard", description="Top XP in this server")
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context):
        rows = self.db.top_xp(ctx.guild.id)
        text = "\n".join(f"`{n}.` <@{r['user_id']}> — Level {r['level']} · {r['amount']} XP" for n, r in enumerate(rows, 1))
        e = discord.Embed(title="🏆 Leaderboard", description=text or "No XP yet.", color=BLURPLE)
        await ctx.reply(embed=e, ephemeral=True, mention_author=False)

    @xp.command(name="add", description="Add XP to a member")
    @admin_only()
    async def xp_add(self, i: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1000000]):
        total, level = self.db.add_xp(i.guild.id, user.id, amount)
        await say(i, f"✅ {user.mention}: `{total}` XP, level `{level}`.")

    @xp.command(name="remove", description="Remove XP from a member")
    @admin_only()
    async def xp_remove(self, i: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1000000]):
        total, level = self.db.add_xp(i.guild.id, user.id, -amount)
        await say(i, f"✅ {user.mention}: `{total}` XP, level `{level}`.")

    # ---- tickets
    @ticket.command(name="setup", description="Enable tickets")
    @admin_only()
    async def ticket_setup(self, i: discord.Interaction):
        self.db.set(i.guild.id, "ticket_setup", True)
        await say(i, "✅ Tickets enabled.")

    @ticket.command(name="create", description="Open a private support ticket")
    async def ticket_create(self, i: discord.Interaction):
        g = i.guild
        if not g.me.guild_permissions.manage_channels:
            return await say(i, "❌ I need Manage Channels.")
        category = discord.utils.get(g.categories, name=TICKET_CATEGORY) or await g.create_category(TICKET_CATEGORY)
        overwrites = {
            g.default_role: discord.PermissionOverwrite(view_channel=False),
            g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
            i.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        ch = await g.create_text_channel(f"ticket-{i.user.name[:20]}", category=category, overwrites=overwrites)
        await say(i, f"✅ Created {ch.mention}.")

    def is_ticket(self, ch: discord.abc.GuildChannel) -> bool:
        return getattr(ch.category, "name", None) == TICKET_CATEGORY

    @ticket.command(name="close", description="Close (delete) a ticket channel")
    @admin_only()
    async def ticket_close(self, i: discord.Interaction, channel: discord.TextChannel | None = None):
        ch = channel or i.channel
        if not isinstance(ch, discord.TextChannel) or not self.is_ticket(ch):
            return await say(i, "❌ That isn't a ticket channel.")
        await say(i, "✅ Ticket closed.")
        await ch.delete(reason=f"Closed by {i.user}")

    @ticket.command(name="add", description="Add a member to a ticket")
    @admin_only()
    async def ticket_add(self, i: discord.Interaction, user: discord.Member, channel: discord.TextChannel | None = None):
        ch = channel or i.channel
        if not isinstance(ch, discord.TextChannel) or not self.is_ticket(ch):
            return await say(i, "❌ That isn't a ticket channel.")
        await ch.set_permissions(user, view_channel=True, send_messages=True)
        await say(i, "✅ User added.")

    @ticket.command(name="remove", description="Remove a member from a ticket")
    @admin_only()
    async def ticket_remove(self, i: discord.Interaction, user: discord.Member, channel: discord.TextChannel | None = None):
        ch = channel or i.channel
        if not isinstance(ch, discord.TextChannel) or not self.is_ticket(ch):
            return await say(i, "❌ That isn't a ticket channel.")
        await ch.set_permissions(user, overwrite=None)
        await say(i, "✅ User removed.")

    # ---- logging
    @app_commands.command(name="loggingsetup", description="Enable logging")
    @app_commands.guild_only()
    @admin_only()
    async def loggingsetup(self, i: discord.Interaction):
        self.db.set(i.guild.id, "logging_enabled", True)
        await say(i, "✅ Logging enabled.")

    async def set_log(self, i: discord.Interaction, channel: discord.TextChannel, category: str):
        self.db.set(i.guild.id, f"log_{category.lower()}", channel.id)
        await say(i, f"✅ `{category}` logs → {channel.mention}.")

    @logging.command(name="set", description="Send a log category to a channel")
    @admin_only()
    async def logging_set(self, i: discord.Interaction, channel: discord.TextChannel, category: str = "general"):
        await self.set_log(i, channel, category)

    @logging.command(name="securitylog", description="Set the security log channel")
    @admin_only()
    async def logging_security(self, i: discord.Interaction, channel: discord.TextChannel):
        await self.set_log(i, channel, "security")

    @logging.command(name="modlog", description="Set the moderation log channel")
    @admin_only()
    async def logging_mod(self, i: discord.Interaction, channel: discord.TextChannel):
        await self.set_log(i, channel, "mod")

    @app_commands.command(name="securitylog", description="Set the security log channel")
    @app_commands.guild_only()
    @admin_only()
    async def securitylog(self, i: discord.Interaction, channel: discord.TextChannel):
        await self.set_log(i, channel, "security")

    @app_commands.command(name="modlog", description="Set the moderation log channel")
    @app_commands.guild_only()
    @admin_only()
    async def modlog(self, i: discord.Interaction, channel: discord.TextChannel):
        await self.set_log(i, channel, "mod")

    # ---- notifications (settings only, as in the current repo)
    async def notifier(self, i: discord.Interaction, kind: str, channel: discord.TextChannel, handle: str):
        self.db.set(i.guild.id, f"notify_{kind}", {"channel": channel.id, "handle": handle})
        await say(i, f"✅ {kind.title()} notifier saved: `{handle}` → {channel.mention}.")

    @app_commands.command(name="youtubenotifiersetup", description="Set up YouTube notifications")
    @app_commands.guild_only()
    @admin_only()
    async def youtubenotifiersetup(self, i: discord.Interaction, channel: discord.TextChannel, handle: str):
        await self.notifier(i, "youtube", channel, handle)

    @app_commands.command(name="tiktoknotifiersetup", description="Set up TikTok notifications")
    @app_commands.guild_only()
    @admin_only()
    async def tiktoknotifiersetup(self, i: discord.Interaction, channel: discord.TextChannel, handle: str):
        await self.notifier(i, "tiktok", channel, handle)

    @app_commands.command(name="instagramnotifiersetup", description="Set up Instagram notifications")
    @app_commands.guild_only()
    @admin_only()
    async def instagramnotifiersetup(self, i: discord.Interaction, channel: discord.TextChannel, handle: str):
        await self.notifier(i, "instagram", channel, handle)

    # ---- voice / music
    @app_commands.command(name="voicemastersetup", description="Enable VoiceMaster")
    @app_commands.guild_only()
    @admin_only()
    async def voicemastersetup(self, i: discord.Interaction):
        self.db.set(i.guild.id, "voicemaster", True)
        await say(i, "✅ VoiceMaster enabled.")

    @app_commands.command(name="greetvoicesetup", description="Voice greeting: new members get a role, then are welcomed by voice")
    @app_commands.describe(channel="Voice channel new members should join", role="Role given on join, removed after the greeting",
                           text="What the bot says. Use {user}, {server}, {count}")
    @app_commands.guild_only()
    @admin_only()
    async def greetvoicesetup(self, i: discord.Interaction, channel: discord.VoiceChannel, role: discord.Role, text: str):
        self.db.save_guild(i.guild.id, values={"greet_voice": channel.id, "greet_role": role.id, "greet_text": text})
        await say(i, f"✅ Greet voice saved. New members get {role.mention}; when they join {channel.mention} the bot "
                     f"greets them by voice and removes the role. Keep my role above {role.mention}.")

    @app_commands.command(name="welcomesetup", description="Send a welcome message when someone joins")
    @app_commands.describe(channel="Where to post it", message="Optional. Use {user}, {server}, {count}")
    @app_commands.guild_only()
    @admin_only()
    async def welcomesetup(self, i: discord.Interaction, channel: discord.TextChannel, message: str = DEFAULT_WELCOME):
        self.db.save_guild(i.guild.id, values={"welcome_channel": channel.id, "welcome_text": message})
        await say(i, f"✅ Welcome messages → {channel.mention}.\nPreview: {self.fill(message, i.user)}")

    @app_commands.command(name="welcomeoff", description="Turn welcome messages off")
    @app_commands.guild_only()
    @admin_only()
    async def welcomeoff(self, i: discord.Interaction):
        self.db.set(i.guild.id, "welcome_channel", None)
        await say(i, "✅ Welcome messages disabled.")

    @app_commands.command(name="play", description="Queue a track")
    async def play(self, i: discord.Interaction, query: str):
        await say(i, f"🎵 Queued `{query}`. Install FFmpeg and configure an audio provider to enable playback.")

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, i: discord.Interaction):
        await say(i, "⏸ Playback paused.")

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, i: discord.Interaction):
        await say(i, "▶ Playback resumed.")

    @app_commands.command(name="queue", description="Show the queue")
    async def queue(self, i: discord.Interaction):
        await say(i, "🎶 Queue is empty.")

    # ---- owner
    @owner.command(name="stats", description="Bot statistics (owner only)")
    async def stats(self, i: discord.Interaction):
        if i.user.id not in OWNER_IDS:
            return await say(i, "Owner-only command.")
        users = sum(g.member_count or 0 for g in self.bot.guilds)
        await say(i, f"Guilds: `{len(self.bot.guilds)}` · Users: `{users}`")


# ---------------------------------------------------------------- bot
def resolve_prefix(bot: "PurifyBot", message: discord.Message):
    custom = bot.db.guild(message.guild.id)["prefix"] if message.guild else PREFIX
    return commands.when_mentioned_or(custom, PREFIX)(bot, message)


class PurifyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        self.db = Database(DB_URL, PREFIX)
        super().__init__(command_prefix=resolve_prefix, intents=intents, help_command=None)
        self.tree.on_error = self.on_tree_error

    async def setup_hook(self):
        await self.add_cog(Core(self))
        await self.tree.sync()

    async def on_ready(self):
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="/help | Protecting servers"))
        LOG.info("PURIFY online: %d servers", len(self.guilds))

    @staticmethod
    def friendly(error: Exception) -> str:
        while getattr(error, "original", None) is not None:
            error = error.original
        if isinstance(error, (commands.MissingPermissions, app_commands.MissingPermissions)):
            return "❌ You don't have the required permission."
        if isinstance(error, (commands.NoPrivateMessage, app_commands.NoPrivateMessage)):
            return "❌ This only works in a server."
        if isinstance(error, discord.Forbidden):
            return "❌ I'm missing permissions or my role is too low."
        if isinstance(error, commands.UserInputError):
            return "❌ Invalid arguments. Use `/help` to see how the command works."
        if isinstance(error, (commands.CheckFailure, app_commands.CheckFailure)):
            return "❌ You can't use that here."
        LOG.error("Command error", exc_info=error)
        return "❌ Command failed. Check permissions and arguments."

    async def on_tree_error(self, i: discord.Interaction, error: app_commands.AppCommandError):
        await say(i, self.friendly(error))

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if not isinstance(error, commands.CommandNotFound):
            await ctx.reply(self.friendly(error), mention_author=False, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN (see .env.example).")
    PurifyBot().run(TOKEN, log_handler=None)
