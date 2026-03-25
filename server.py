import asyncio
import base64
import json
import logging
import os
import re
import secrets
import signal
import time
import traceback
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
SECRET_FIELDS = {
    "api_key", "token", "app_secret", "encrypt_key",
    "verification_token", "bot_token", "app_token",
    "channel_secret", "channel_access_token", "client_secret",
}

CONFIG_DIR = Path(os.environ.get("PICOCLAW_HOME", Path.home() / ".picoclaw"))
CONFIG_PATH = CONFIG_DIR / "config.json"

# Shared log buffer — used by both the logging handler and gateway subprocess reader
LOG_BUFFER: deque[str] = deque(maxlen=2000)
_log_lock = asyncio.Lock()


def _ts() -> str:
    """Return a UTC timestamp string for log lines."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _append_log(line: str):
    """Thread-safe append to the shared log buffer."""
    LOG_BUFFER.append(f"[{_ts()}] {line}")


class _BufferLogHandler(logging.Handler):
    """Pipe Python logging into the shared log buffer."""

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            _append_log(msg)
        except Exception:
            pass


# Set up root logger to capture server-level logs (config errors, startup, etc.)
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.DEBUG)
_log_fmt = logging.Formatter("%(levelname)s %(name)s: %(message)s")
_buffer_handler = _BufferLogHandler()
_buffer_handler.setFormatter(_log_fmt)
_root_logger.addHandler(_buffer_handler)
# Also log to stderr so Railway/log aggregators can see app-level messages
_stderr_handler = logging.StreamHandler()
_stderr_handler.setFormatter(_log_fmt)
_root_logger.addHandler(_stderr_handler)
# Reduce noise from third-party loggers
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"Generated admin password: {ADMIN_PASSWORD}")


class BasicAuthBackend(AuthenticationBackend):
    async def authenticate(self, conn):
        if "Authorization" not in conn.headers:
            return None

        auth = conn.headers["Authorization"]
        try:
            scheme, credentials = auth.split()
            if scheme.lower() != "basic":
                return None
            decoded = base64.b64decode(credentials).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            raise AuthenticationError("Invalid credentials")

        username, _, password = decoded.partition(":")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            return AuthCredentials(["authenticated"]), SimpleUser(username)

        raise AuthenticationError("Invalid credentials")


def require_auth(request: Request):
    if not request.user.is_authenticated:
        return PlainTextResponse(
            "Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="picoclaw"'},
        )
    return None


def load_config():
    if not CONFIG_PATH.exists():
        return default_config()
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return default_config()


def save_config(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))


def apply_env_overrides(config):
    """Override provider settings from environment variables.

    Env vars follow the pattern: PICOCLAW_PROVIDER_<NAME>_<KEY>
    e.g. PICOCLAW_PROVIDER_ANTHROPIC_API_KEY=key123
         PICOCLAW_PROVIDER_OPENAI_ENABLED=true
         PICOCLAW_PROVIDER_DEEPSEEK_API_BASE=http://...

    Environment values always take precedence over config.json / API values.
    """
    prefix = "PICOCLAW_PROVIDER_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        # Strip prefix and split into provider_name and field
        rest = key[len(prefix):]  # e.g. "ANTHROPIC_API_KEY"
        parts = rest.split("_", 1)
        if len(parts) != 2:
            continue
        provider_name, field_name = parts[0].lower(), parts[1].lower()

        providers = config.get("providers", {})
        if provider_name not in providers:
            continue

        # Parse booleans for "enabled" field
        if field_name == "enabled":
            parsed = value.lower() in ("true", "1", "yes")
        else:
            parsed = value

        providers[provider_name][field_name] = parsed

    # Channel overrides: PICOCLAW_CHANNEL_<NAME>_<KEY>
    # e.g. PICOCLAW_CHANNEL_TELEGRAM_ENABLED=true
    #      PICOCLAW_CHANNEL_TELEGRAM_TOKEN=bot-token-here
    #      PICOCLAW_CHANNEL_TELEGRAM_PROXY=socks5://...
    #      PICOCLAW_CHANNEL_TELEGRAM_ALLOW_FROM=user1,user2
    channel_prefix = "PICOCLAW_CHANNEL_"
    for key, value in os.environ.items():
        if not key.startswith(channel_prefix):
            continue
        rest = key[len(channel_prefix):]
        parts = rest.split("_", 1)
        if len(parts) != 2:
            continue
        channel_name, field_name = parts[0].lower(), parts[1].lower()

        channels = config.get("channels", {})
        if channel_name not in channels:
            continue

        if field_name == "enabled":
            parsed = value.lower() in ("true", "1", "yes")
        elif field_name == "allow_from":
            parsed = [v.strip() for v in value.split(",") if v.strip()]
        else:
            parsed = value

        channels[channel_name][field_name] = parsed

    # Agent defaults: PICOCLAW_DEFAULT_PROVIDER, PICOCLAW_DEFAULT_MODEL
    if "PICOCLAW_DEFAULT_PROVIDER" in os.environ:
        config.setdefault("agents", {}).setdefault("defaults", {})["provider"] = os.environ["PICOCLAW_DEFAULT_PROVIDER"]
    if "PICOCLAW_DEFAULT_MODEL" in os.environ:
        config.setdefault("agents", {}).setdefault("defaults", {})["model"] = os.environ["PICOCLAW_DEFAULT_MODEL"]

    return config


def load_config():
    config = default_config()
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError as e:
            logging.error("Config file %s is invalid JSON: %s", CONFIG_PATH, e)
        except Exception as e:
            logging.error("Failed to read config file %s: %s", CONFIG_PATH, e)
    return apply_env_overrides(config)


def default_config():
    return {
        "agents": {
            "defaults": {
                "workspace": "~/.picoclaw/workspace",
                "restrict_to_workspace": True,
                "provider": "",
                "model": "glm-4.7",
                "max_tokens": 8192,
                "temperature": 0.7,
                "max_tool_iterations": 20,
            }
        },
        "channels": {
            "telegram": {"enabled": False, "token": "", "proxy": "", "allow_from": []},
            "discord": {"enabled": False, "token": "", "allow_from": []},
            "slack": {"enabled": False, "bot_token": "", "app_token": "", "allow_from": []},
            "whatsapp": {"enabled": False, "bridge_url": "ws://localhost:3001", "allow_from": []},
            "feishu": {"enabled": False, "app_id": "", "app_secret": "", "encrypt_key": "", "verification_token": "", "allow_from": []},
            "dingtalk": {"enabled": False, "client_id": "", "client_secret": "", "allow_from": []},
            "qq": {"enabled": False, "app_id": "", "app_secret": "", "allow_from": []},
            "line": {"enabled": False, "channel_secret": "", "channel_access_token": "", "webhook_host": "0.0.0.0", "webhook_port": 18791, "webhook_path": "/webhook/line", "allow_from": []},
            "maixcam": {"enabled": False, "host": "0.0.0.0", "port": 18790, "allow_from": []},
        },
        "providers": {
            "anthropic": {"enabled": False, "api_key": ""},
            "openai": {"enabled": False, "api_key": "", "api_base": ""},
            "openrouter": {"enabled": False, "api_key": ""},
            "deepseek": {"enabled": False, "api_key": ""},
            "groq": {"enabled": False, "api_key": ""},
            "gemini": {"enabled": False, "api_key": ""},
            "zhipu": {"enabled": False, "api_key": "", "api_base": ""},
            "vllm": {"enabled": False, "api_key": "", "api_base": ""},
            "nvidia": {"enabled": False, "api_key": "", "api_base": ""},
            "moonshot": {"enabled": False, "api_key": ""},
        },
        "gateway": {"host": "0.0.0.0", "port": 18790},
        "tools": {
            "web": {
                "brave": {"enabled": False, "api_key": "", "max_results": 5},
                "duckduckgo": {"enabled": True, "max_results": 5},
            }
        },
        "heartbeat": {"enabled": True, "interval": 30},
        "devices": {"enabled": False, "monitor_usb": False},
    }


def mask_secrets(data, _path=""):
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if k in SECRET_FIELDS and isinstance(v, str) and v:
                result[k] = v[:8] + "***" if len(v) > 8 else "***"
            else:
                result[k] = mask_secrets(v, f"{_path}.{k}")
        return result
    if isinstance(data, list):
        return [mask_secrets(item, _path) for item in data]
    return data


def merge_secrets(new_data, existing_data):
    if isinstance(new_data, dict) and isinstance(existing_data, dict):
        result = {}
        for k, v in new_data.items():
            if k in SECRET_FIELDS and isinstance(v, str) and (v.endswith("***") or v == ""):
                result[k] = existing_data.get(k, "")
            else:
                result[k] = merge_secrets(v, existing_data.get(k, {}))
        return result
    return new_data


class GatewayManager:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self.state = "stopped"
        self.start_time: float | None = None
        self.restart_count = 0
        self._read_tasks: list[asyncio.Task] = []

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        self.state = "starting"
        _append_log(f"Starting gateway (restart #{self.restart_count})...")
        try:
            # Write the resolved config (with env overrides) to disk so the
            # gateway subprocess can read it — it doesn't share our memory.
            resolved = load_config()
            save_config(resolved)
            logging.info("Wrote resolved config to %s for gateway subprocess", CONFIG_PATH)

            self.process = await asyncio.create_subprocess_exec(
                "picoclaw", "gateway",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self.state = "running"
            self.start_time = time.time()
            _append_log(f"Gateway started (pid={self.process.pid})")
            task = asyncio.create_task(self._read_output())
            self._read_tasks.append(task)
        except FileNotFoundError:
            self.state = "error"
            _append_log("Failed to start gateway: 'picoclaw' command not found. "
                        "Is picoclaw installed and in PATH?")
            logging.error("'picoclaw' executable not found in PATH", stack_info=True)
        except Exception as e:
            self.state = "error"
            _append_log(f"Failed to start gateway: {type(e).__name__}: {e}")
            logging.error("Failed to start gateway", exc_info=True)

    async def stop(self):
        if not self.process or self.process.returncode is not None:
            self.state = "stopped"
            return
        self.state = "stopping"
        _append_log(f"Stopping gateway (pid={self.process.pid})...")
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
            _append_log("Gateway stopped gracefully")
        except asyncio.TimeoutError:
            _append_log("Gateway did not stop in 10s, killing...")
            self.process.kill()
            await self.process.wait()
            _append_log("Gateway killed")
        self.state = "stopped"
        self.start_time = None

    async def restart(self):
        await self.stop()
        self.restart_count += 1
        await self.start()

    async def _read_output(self):
        try:
            while self.process and self.process.stdout:
                line = await self.process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                cleaned = ANSI_ESCAPE.sub("", decoded)
                if cleaned:
                    LOG_BUFFER.append(f"[{_ts()}] {cleaned}")
        except asyncio.CancelledError:
            return
        # Drain any remaining buffered output (process may have exited before
        # readline could yield all lines, especially on fast crashes)
        if self.process and self.process.stdout:
            remaining = await self.process.stdout.read()
            if remaining:
                for line in remaining.decode("utf-8", errors="replace").splitlines():
                    cleaned = ANSI_ESCAPE.sub("", line).rstrip()
                    if cleaned:
                        LOG_BUFFER.append(f"[{_ts()}] {cleaned}")
        if self.process and self.process.returncode is not None and self.state == "running":
            code = self.process.returncode
            uptime = int(time.time() - self.start_time) if self.start_time else 0
            self.state = "error"
            _append_log(f"Gateway crashed with exit code {code} after {uptime}s")
            if code == 137:
                _append_log("  Hint: exit code 137 usually means the process was killed (OOM or signal)")
            elif code == 1:
                _append_log("  Hint: exit code 1 typically indicates a configuration or startup error")
                if uptime == 0:
                    config = load_config()
                    provider = config.get("agents", {}).get("defaults", {}).get("provider", "")
                    model = config.get("agents", {}).get("defaults", {}).get("model", "")
                    if not provider:
                        _append_log("  Likely cause: no default provider set (agents.defaults.provider is empty)")
                        _append_log("  Fix: set PICOCLAW_DEFAULT_PROVIDER=openrouter in your environment")
                    enabled = [n for n, p in config.get("providers", {}).items()
                               if isinstance(p, dict) and p.get("enabled") and p.get("api_key")]
                    if not enabled:
                        _append_log("  Likely cause: no providers are enabled with API keys")
                    _append_log(f"  Current config: provider={provider!r}, model={model!r}, "
                                f"enabled_providers={enabled}")
            logging.error("Gateway process exited unexpectedly: code=%s uptime=%ds", code, uptime)

    def get_status(self) -> dict:
        pid = None
        if self.process and self.process.returncode is None:
            pid = self.process.pid
        uptime = None
        if self.start_time and self.state == "running":
            uptime = int(time.time() - self.start_time)
        return {
            "state": self.state,
            "pid": pid,
            "uptime": uptime,
            "restart_count": self.restart_count,
        }


gateway = GatewayManager()
config_lock = asyncio.Lock()


async def homepage(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    return templates.TemplateResponse(request, "index.html")


async def health(request: Request):
    return JSONResponse({"status": "ok", "gateway": gateway.state})


async def api_config_get(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    config = load_config()
    return JSONResponse(mask_secrets(config))


async def api_config_put(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    try:
        restart = body.pop("_restartGateway", False)

        async with config_lock:
            existing = load_config()
            merged = merge_secrets(body, existing)
            save_config(merged)

        if restart:
            asyncio.create_task(gateway.restart())

        return JSONResponse({"ok": True, "restarting": restart})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_status(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err

    config = load_config()

    providers = {}
    for name, prov in config.get("providers", {}).items():
        providers[name] = {"enabled": prov.get("enabled", False), "configured": bool(prov.get("api_key"))}

    channels = {}
    for name, chan in config.get("channels", {}).items():
        channels[name] = {"enabled": chan.get("enabled", False)}

    cron_dir = CONFIG_DIR / "cron"
    cron_jobs = []
    if cron_dir.exists():
        for f in cron_dir.glob("*.json"):
            try:
                cron_jobs.append(json.loads(f.read_text()))
            except Exception:
                pass

    return JSONResponse({
        "gateway": gateway.get_status(),
        "providers": providers,
        "channels": channels,
        "cron": {"count": len(cron_jobs), "jobs": cron_jobs},
    })


async def api_logs(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    return JSONResponse({"lines": list(LOG_BUFFER)})


async def api_gateway_start(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    asyncio.create_task(gateway.start())
    return JSONResponse({"ok": True})


async def api_gateway_stop(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    asyncio.create_task(gateway.stop())
    return JSONResponse({"ok": True})


async def api_gateway_restart(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    asyncio.create_task(gateway.restart())
    return JSONResponse({"ok": True})


async def auto_start_gateway():
    config = load_config()
    enabled_providers = []
    for name, prov in config.get("providers", {}).items():
        if isinstance(prov, dict) and prov.get("enabled") and prov.get("api_key"):
            enabled_providers.append(name)
    if enabled_providers:
        _append_log(f"Auto-starting gateway with providers: {', '.join(enabled_providers)}")
        logging.info("Auto-starting gateway with providers: %s", ", ".join(enabled_providers))
        asyncio.create_task(gateway.start())
    else:
        _append_log("Gateway not auto-started: no providers enabled with API keys configured")
        logging.info("No enabled providers with API keys; skipping auto-start")


routes = [
    Route("/", homepage),
    Route("/health", health),
    Route("/api/config", api_config_get, methods=["GET"]),
    Route("/api/config", api_config_put, methods=["PUT"]),
    Route("/api/status", api_status),
    Route("/api/logs", api_logs),
    Route("/api/gateway/start", api_gateway_start, methods=["POST"]),
    Route("/api/gateway/stop", api_gateway_stop, methods=["POST"]),
    Route("/api/gateway/restart", api_gateway_restart, methods=["POST"]),
]

@asynccontextmanager
async def lifespan(app):
    await auto_start_gateway()
    yield
    await gateway.stop()


app = Starlette(
    routes=routes,
    middleware=[Middleware(AuthenticationMiddleware, backend=BasicAuthBackend())],
    lifespan=lifespan,
)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)

    def handle_signal():
        loop.create_task(gateway.stop())
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    loop.run_until_complete(server.serve())
