import asyncio
import aiohttp
import base64
import json
import os
import signal
import sys
import time
import uuid
import threading
from aiohttp import web
from typing import Dict, Optional

# Load .env file if present
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_ENV_PATH):
    try:
        with open(_ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("\"'")
                    if k not in os.environ:
                        os.environ[k] = v
    except Exception as ex:
        print(f"[warning] Failed reading .env: {ex}")

SCIENCE_URL = "https://discord.com/api/v9/science"
DETECTABLE_URL = "https://discord.com/api/v10/applications/detectable"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) discord/1.0.9253 Chrome/148.0.7778.280 "
    "Electron/42.7.1 Safari/537.36"
)
CLIENT_VER   = "1.0.9253"
BUILD_NUM    = 594031
NATIVE_BUILD = 88414
OS_VER       = "10.0.26200"

# Caches for detectable games
_DETECTABLE_GAMES: Dict[str, str] = {}       # name_lower -> application_id
_DETECTABLE_GAMES_EXE: Dict[str, str] = {}   # application_id -> win32 exe name


def make_super_properties() -> str:
    """Builds base64-encoded X-Super-Properties simulating the Discord Windows Desktop client."""
    props = {
        "os": "Windows",
        "browser": "Discord Client",
        "release_channel": "stable",
        "client_version": CLIENT_VER,
        "os_version": OS_VER,
        "os_arch": "x64",
        "app_arch": "x64",
        "system_locale": "en-US",
        "has_client_mods": False,
        "browser_user_agent": USER_AGENT,
        "browser_version": "42.7.1",
        "client_build_number": BUILD_NUM,
        "native_build_number": NATIVE_BUILD,
        "client_event_source": None,
        "client_app_state": "focused",
        "client_launch_id": str(uuid.uuid4()),
        "launch_signature": str(uuid.uuid4()),
        "client_heartbeat_session_id": str(uuid.uuid4()),
    }
    raw = json.dumps(props, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("utf-8")


async def load_detectable_games(session: aiohttp.ClientSession, token: str) -> None:
    """Fetches Discord's verified database of detectable games."""
    global _DETECTABLE_GAMES, _DETECTABLE_GAMES_EXE
    if _DETECTABLE_GAMES:
        return

    headers = {"Authorization": token, "User-Agent": USER_AGENT}
    try:
        async with session.get(DETECTABLE_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                print(f"[!] Failed to fetch detectable games (HTTP {r.status})")
                return
            data = await r.json()
            if not isinstance(data, list):
                return

            mapping = {}
            exe_mapping = {}
            for app in data:
                if not isinstance(app, dict):
                    continue
                aid = str(app.get("id") or "")
                name = (app.get("name") or "").strip()
                if aid and name:
                    mapping[name.lower()] = aid
                for alias in app.get("aliases") or []:
                    if isinstance(alias, str) and alias.strip():
                        mapping[alias.strip().lower()] = aid
                for exe_entry in app.get("executables") or []:
                    if (isinstance(exe_entry, dict)
                            and exe_entry.get("os") == "win32"
                            and exe_entry.get("name")):
                        exe_mapping[aid] = exe_entry["name"]
                        break

            _DETECTABLE_GAMES.update(mapping)
            _DETECTABLE_GAMES_EXE.update(exe_mapping)
            print(f"[*] Loaded {len(_DETECTABLE_GAMES)} detectable games from Discord ({len(_DETECTABLE_GAMES_EXE)} with exe data).")
    except Exception as ex:
        print(f"[!] Error loading detectable games: {ex}")


def match_game(game_name: str):
    """Finds matching application_id and executable name for a given game title."""
    if not game_name or not _DETECTABLE_GAMES:
        return None, "game.exe"
    key = game_name.lower().strip()

    # Exact match
    if key in _DETECTABLE_GAMES:
        aid = _DETECTABLE_GAMES[key]
        return aid, _DETECTABLE_GAMES_EXE.get(aid, "game.exe")

    # Prefix match
    for gname, aid in _DETECTABLE_GAMES.items():
        if gname == key or gname.startswith(key) or key.startswith(gname):
            return aid, _DETECTABLE_GAMES_EXE.get(aid, "game.exe")

    # Substring match
    for gname, aid in _DETECTABLE_GAMES.items():
        if key in gname or gname in key:
            return aid, _DETECTABLE_GAMES_EXE.get(aid, "game.exe")

    return None, "game.exe"


class DiscordBadgeTracker:
    """Manages the full lifecycle of a verified Discord Game telemetry session."""

    def __init__(self, token: str, cookie: str):
        self.token = token
        self.cookie = cookie
        self.analytics_token: Optional[str] = None
        self.analytics_token_ts: float = 0.0

        self.session: Optional[aiohttp.ClientSession] = None
        self.hb_task: Optional[asyncio.Task] = None

        self.game_id = ""
        self.game_name = ""
        self.game_exe = ""
        self.start_ts = 0.0
        self.hb_session = ""
        self.launch_sig = ""
        self.session_id = ""

    async def init_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def fetch_analytics_token(self) -> Optional[str]:
        now = time.time()
        if self.analytics_token and (now - self.analytics_token_ts) < (6 * 3600):
            return self.analytics_token

        await self.init_session()
        headers = {"Authorization": self.token, "User-Agent": USER_AGENT}
        try:
            async with self.session.get(
                "https://discord.com/api/v9/users/@me?with_analytics_token=true",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=12)
            ) as r:
                data = await r.json()
            tok = data.get("analytics_token")
            if tok:
                self.analytics_token = tok
                self.analytics_token_ts = now
                return tok
            print(f"[!] No analytics_token returned (HTTP {r.status})")
        except Exception as ex:
            print(f"[!] Analytics token fetch failed: {ex}")
        return None

    async def post_science(self, events: list) -> bool:
        if not self.cookie:
            print("[!] Cookie not set. Discord requires cookies for /science telemetry.")
            return False

        a_token = await self.fetch_analytics_token()
        if not a_token:
            return False

        headers = {
            "Authorization": self.token,
            "Content-Type": "application/json",
            "Cookie": self.cookie,
            "User-Agent": USER_AGENT,
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/channels/@me",
            "X-Debug-Options": "bugReporterEnabled",
            "X-Discord-Locale": "en-US",
            "X-Discord-Timezone": "America/New_York",
            "X-Super-Properties": make_super_properties(),
        }
        payload = {"token": a_token, "events": events}

        await self.init_session()
        try:
            async with self.session.post(
                SCIENCE_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status == 204:
                    return True
                body = await r.text()
                print(f"[science] HTTP {r.status}: {body[:150]}")
                if r.status in (401, 403):
                    print("[!] Cookie rejected or expired. Please update DISCORD_COOKIE.")
                return False
        except Exception as ex:
            print(f"[!] /science POST failed: {ex}")
            return False

    async def start_game(self, game_name: str) -> bool:
        """Starts tracking a game by title or verified Application ID."""
        await self.init_session()
        await load_detectable_games(self.session, self.token)

        # Stop existing session if running
        await self.stop_game(send_final=True)

        # Resolve game to verified ID
        app_id, exe_name = match_game(game_name)
        if not app_id:
            print(f"[!] '{game_name}' was not found in Discord's detectable games database.")
            print("[!] Discord badges only track games verified in their database.")
            return False

        self.game_id = app_id
        self.game_name = game_name
        self.game_exe = exe_name
        self.hb_session = str(uuid.uuid4())
        self.launch_sig = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.start_ts = time.time()
        now_ms = int(time.time() * 1000)

        # Open events bundle: running_game_heartbeat + launch_game
        open_events = [
            {
                "type": "running_game_heartbeat",
                "properties": {
                    "client_track_timestamp": now_ms,
                    "client_heartbeat_session_id": self.hb_session,
                    "event_sequence_number": 1,
                    "game_id": self.game_id,
                    "game_name": self.game_name,
                    "game_metadata": None,
                    "game_executable": self.game_exe,
                    "game_detection_enabled": True,
                    "initial_heartbeat": True,
                    "final_heartbeat": False,
                    "game_session_id": self.session_id,
                    "duration_tracked_ms": 0,
                    "rtc_connection_id": None,
                    "media_session_id": None,
                    "launch_signature": self.launch_sig,
                    "client_app_state": "focused",
                    "client_send_timestamp": now_ms,
                },
            },
            {
                "type": "launch_game",
                "properties": {
                    "client_track_timestamp": now_ms,
                    "client_heartbeat_session_id": self.hb_session,
                    "event_sequence_number": 2,
                    "game": self.game_name,
                    "game_id": self.game_id,
                    "verified": True,
                    "elevated": False,
                    "is_launcher": False,
                    "game_platform": "desktop",
                    "detection_method": "verified_game",
                    "is_overlay_enabled": False,
                    "is_overlay_game_enabled": True,
                    "is_overlay_game_source": "OOP_DEFAULT_DATABASE",
                    "fullscreen_type": "UNKNOWN",
                    "hardware_display_count": 1,
                    "overlay_method": "Disabled",
                    "activity_status_enabled": True,
                    "activity_status_shared_guilds": [],
                    "current_user_status": "online",
                    "game_detection_enabled": True,
                    "executable_path": self.game_exe,
                    "voice_channel_id": None,
                    "voice_channel_type": None,
                    "voice_channel_bitrate": None,
                    "voice_channel_guild_id": None,
                    "hidden_by_distributor": False,
                    "game_metadata": None,
                    "client_performance_cpu": None,
                    "client_performance_memory": None,
                    "cpu_core_count": None,
                    "accessibility_features": 0,
                    "rendered_locale": "en-US",
                    "launch_signature": self.launch_sig,
                    "client_rtc_state": None,
                    "client_app_state": "focused",
                    "client_send_timestamp": now_ms,
                },
            },
        ]

        ok = await self.post_science(open_events)
        print(f"[science] Session opened for '{self.game_name}' (ID: {self.game_id}, Exe: {self.game_exe}) â€” {'OK' if ok else 'FAILED'}")

        # Start 120s heartbeat loop
        self.hb_task = asyncio.create_task(self._heartbeat_loop(), name="badge-hb")
        return ok

    async def _heartbeat_loop(self):
        """Dispatches running_game_heartbeat every 120 seconds matching Discord desktop cadence."""
        seq = 3
        try:
            while True:
                await asyncio.sleep(120)
                elapsed_ms = int((time.time() - self.start_ts) * 1000)
                now_ms = int(time.time() * 1000)
                hb = {
                    "type": "running_game_heartbeat",
                    "properties": {
                        "client_track_timestamp": now_ms,
                        "client_heartbeat_session_id": self.hb_session,
                        "event_sequence_number": seq,
                        "game_id": self.game_id,
                        "game_name": self.game_name,
                        "game_metadata": None,
                        "game_executable": self.game_exe,
                        "game_detection_enabled": True,
                        "initial_heartbeat": False,
                        "final_heartbeat": False,
                        "game_session_id": self.session_id,
                        "duration_tracked_ms": elapsed_ms,
                        "rtc_connection_id": None,
                        "media_session_id": None,
                        "launch_signature": self.launch_sig,
                        "client_app_state": "focused",
                        "client_send_timestamp": now_ms,
                    },
                }
                seq += 1
                ok = await self.post_science([hb])
                hrs = elapsed_ms / 3_600_000
                print(f"[science] Heartbeat â€” '{self.game_name}' {hrs:.2f}h elapsed â€” {'OK' if ok else 'FAILED'}")
        except asyncio.CancelledError:
            pass

    async def stop_game(self, send_final: bool = True):
        """Cancels heartbeats and cleanly closes the session with final_heartbeat=True."""
        if self.hb_task and not self.hb_task.done():
            self.hb_task.cancel()
            try:
                await self.hb_task
            except asyncio.CancelledError:
                pass
        self.hb_task = None

        if not send_final or not self.game_id or not self.start_ts:
            self.game_id = ""
            return

        elapsed_ms = int((time.time() - self.start_ts) * 1000)
        if elapsed_ms < 60_000:
            print("[science] Session was under 60 seconds â€” skipped final close packet.")
            self.game_id = ""
            return

        now_ms = int(time.time() * 1000)
        close_event = {
            "type": "running_game_heartbeat",
            "properties": {
                "client_track_timestamp": now_ms,
                "client_heartbeat_session_id": self.hb_session,
                "event_sequence_number": 9999,
                "game_id": self.game_id,
                "game_name": self.game_name,
                "game_metadata": None,
                "game_executable": self.game_exe,
                "game_detection_enabled": True,
                "initial_heartbeat": False,
                "final_heartbeat": True,
                "game_session_id": self.session_id,
                "duration_tracked_ms": elapsed_ms,
                "rtc_connection_id": None,
                "media_session_id": None,
                "launch_signature": self.launch_sig,
                "client_app_state": "focused",
                "client_send_timestamp": now_ms,
            },
        }

        ok = await self.post_science([close_event])
        hrs = elapsed_ms / 3_600_000
        print(f"[science] Session closed â€” '{self.game_name}' {hrs:.2f}h â€” {'CREDITED' if ok else 'FAILED'}")

        self.game_id = ""
        self.game_name = ""
        self.game_exe = ""
        self.start_ts = 0.0

    async def close(self):
        await self.stop_game(send_final=True)
        if self.session and not self.session.closed:
            await self.session.close()

async def start_discord_listener(tracker: DiscordBadgeTracker, token: str):
    """Connects to the Discord Gateway to listen for .cookie and .new commands."""
    gateway_url = "wss://gateway.discord.gg/?v=9&encoding=json"
    
    # We must identify to receive our own messages
    payload = {
        "op": 2,
        "d": {
            "token": token,
            "properties": {"$os": "windows", "$browser": "chrome", "$device": ""},
            "compress": False
        }
    }

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(gateway_url) as ws:
            # Receive HELLO
            hello = await ws.receive_json()
            heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000

            # Start heartbeat loop in background
            async def heartbeat():
                while True:
                    await asyncio.sleep(heartbeat_interval)
                    await ws.send_json({"op": 1, "d": None})
            
            hb_task = asyncio.create_task(heartbeat())
            
            # Send IDENTIFY
            await ws.send_json(payload)

            # Listen for messages
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = msg.json()
                    
                    if data.get("op") == 0 and data.get("t") == "MESSAGE_CREATE":
                        content = data["d"].get("content", "").strip()
                        channel_id = data["d"]["channel_id"]

                        # Command: .cookie
                        if content == ".cookie":
                            if tracker.hb_task and not tracker.hb_task.done():
                                # It's working
                                elapsed = (time.time() - tracker.start_ts) / 3600
                                status = f" ACTIVE. Farming `{tracker.game_name}` for {elapsed:.2f} hours."
                            else:
                                # Not working
                                status = " STOPPED. The bot is not sending heartbeats."
                            
                            await session.post(
                                f"https://discord.com/api/v9/channels/{channel_id}/messages",
                                headers={"Authorization": token},
                                json={"content": status}
                            )

                        # Command: .new <cookie>
                        elif content.startswith(".new "):
                            new_cookie = content[5:].strip()
                            if new_cookie:
                                tracker.cookie = new_cookie
                                # Restart the game session with the new cookie
                                await tracker.stop_game()
                                await tracker.start_game(tracker.game_name)
                                await session.post(
                                    f"https://discord.com/api/v9/channels/{channel_id}/messages",
                                    headers={"Authorization": token},
                                    json={"content": " Cookie updated. Restarting session..."}
                                )

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
            
            hb_task.cancel()

async def health_check(request):
    return web.Response(text="Bot is alive")


# â”€â”€ Standalone CLI Runner â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async def main():
    token = os.getenv("token") # Your main Discord user token
    if not token:
        print(" TOKEN environment variable not set.")
        exit(1)
    cookie = "Cskzw45-KmQzV14m4vyi1h-r89HAjxJKby6Q3IAf1gxcVZO5Xb1FruFEv8s6vR1Ngk9lggnfS_8h9MuefqNsiE4JUxP7sreG8NTCf51qmIzxBWyct0UDtujknbYTY87mUz0j8xkDqs2JztQOyYkj_-Ma4tOU-MwyXBINf0eycwMos8UoKnG2J6b5c3nwLkANsHJJVFAT_kRdCmKBWM-t6NU0UKvaOsaND9pFjfEghtKAIeieYZ8-4j_2k-OZx_12DrPprKukGQ7Iwhn1Md1lFsghmebxV9laJ5RP2g3nvpd0fg"

    if not token:
        print("ERROR: USER_TOKEN is required. Set it in .env or environment.")
        return
    if not cookie:
        print("ERROR: DISCORD_COOKIE is required. Set it in .env or environment.")
        print("Example: DISCORD_COOKIE=\"__dcfduid=X; __sdcfduid=X; cf_clearance=X; locale=en-GB\"")
        return
    
    # Choose game name from CLI args or default to Grand Theft Auto V
    game = sys.argv[1] if len(sys.argv) > 1 else "Grand Theft Auto V"

    tracker = DiscordBadgeTracker(token, cookie)

    asyncio.create_task(start_discord_listener(tracker, token))

    # Handle graceful exit on Ctrl+C
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _sig_handler():
        stop_event.set()

    for s in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if s:
            try:
                loop.add_signal_handler(s, _sig_handler)
            except NotImplementedError:
                pass  # Windows event loop fallback

    print(f"[*] Starting telemetry badge tracker for: '{game}'")
    print("[*] Press Ctrl+C at any time to cleanly close the session and credit hours.\n")

    started = await tracker.start_game(game)
    if not started:
        await tracker.close()
        return

    try:
        while not stop_event.is_set():
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        print("\n[*] Exiting... cleanly closing telemetry session.")
        await tracker.close()
        print("[*] Done. Hours queued for Discord's next badge sync batch.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
