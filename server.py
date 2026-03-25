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
import yaml

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
SECRET_FIELDS = {
    "api_key", "token", "app_secret", "encrypt_key",
    "verification_token", "bot_token", "app_token",
    "channel_secret", "channel_access_token", "client_secret",
}

CONFIG_DIR = Path(os.environ.get("PICOCLAW_HOME", Path.home() / ".picoclaw"))
CONFIG_PATH = CONFIG_DIR / "config.json"
SECURITY_PATH = CONFIG_DIR / ".security.yml"

# Channel fields that must be written to .security.yml (Go gateway reads tokens
# from there, not from config.json — the token field is unexported in Go).
_CHANNEL_SECRET_FIELDS = {
    "discord": ["token"],
    "telegram": ["token"],
    "slack": ["bot_token", "app_token"],
    "feishu": ["app_secret", "encrypt_key", "verification_token"],
    "dingtalk": ["client_secret"],
    "qq": ["app_secret"],
    "line": ["channel_secret", "channel_access_token"],
    "weixin": ["token"],
    "whatsapp": ["bridge_url"],
}

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


def sync_security_config(config):
    """Extract sensitive channel fields from config and write to .security.yml.

    The Go gateway reads tokens from .security.yml (SecurityConfig), not from
    config.json — the token field is an unexported Go struct field with no json
    tag, so it can never be deserialized from JSON.  This function bridges that
    gap by pulling secret values out of config.json and writing them to the YAML
    file that the Go side actually reads.
    """
    channels = config.get("channels", {})
    sec_channels = {}

    for chan_name, secret_fields in _CHANNEL_SECRET_FIELDS.items():
        chan_cfg = channels.get(chan_name, {})
        if not isinstance(chan_cfg, dict):
            continue
        entry = {}
        for field in secret_fields:
            value = chan_cfg.get(field, "")
            if value:
                entry[field] = value
        if entry:
            sec_channels[chan_name] = entry

    if not sec_channels:
        return

    # Merge with existing .security.yml so we don't clobber model_list keys etc.
    existing_sec = {}
    if SECURITY_PATH.exists():
        try:
            existing_sec = yaml.safe_load(SECURITY_PATH.read_text()) or {}
        except Exception:
            pass

    if "channels" not in existing_sec:
        existing_sec["channels"] = {}
    for chan_name, entry in sec_channels.items():
        if chan_name not in existing_sec["channels"]:
            existing_sec["channels"][chan_name] = {}
        existing_sec["channels"][chan_name].update(entry)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SECURITY_PATH.write_text(yaml.dump(existing_sec, default_flow_style=False, allow_unicode=True))
    logging.info("Synced %d channel(s) to %s", len(sec_channels), SECURITY_PATH)


def save_config(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    sync_security_config(data)


def apply_env_overrides(config):
    """Override config from environment variables.

    Model configuration (recommended):
        PICOCLAW_MODEL_<NAME>_MODEL = "openrouter/anthropic/claude-sonnet-4"
        PICOCLAW_MODEL_<NAME>_API_KEY = "sk-..."
        PICOCLAW_MODEL_<NAME>_API_BASE = "https://..." (optional)
        PICOCLAW_DEFAULT_MODEL_NAME = "<NAME>"

    Legacy provider format (kept for UI compatibility):
        PICOCLAW_PROVIDER_<PROVIDER>_API_KEY = "sk-..."
        PICOCLAW_PROVIDER_<PROVIDER>_API_BASE = "https://..."

    Channel configuration:
        PICOCLAW_CHANNEL_<NAME>_ENABLED = "true"
        PICOCLAW_CHANNEL_<NAME>_TOKEN = "..."
        PICOCLAW_CHANNEL_<NAME>_ALLOW_FROM = "user1,user2"

    Environment values always take precedence over config.json / API values.
    """
    # Build model_list entries from PICOCLAW_MODEL_<NAME>_* env vars
    model_entries = {}  # model_name -> entry dict
    model_prefix = "PICOCLAW_MODEL_"
    for key, value in os.environ.items():
        if not key.startswith(model_prefix):
            continue
        rest = key[len(model_prefix):]  # e.g. "OPENROUTER_MODEL" or "OPENROUTER_API_KEY"
        parts = rest.split("_", 1)
        if len(parts) != 2:
            continue
        model_name, field = parts[0].lower(), parts[1].lower()

        if model_name not in model_entries:
            model_entries[model_name] = {"model_name": model_name}

        if field == "model":
            model_entries[model_name]["model"] = value
        elif field == "api_key":
            model_entries[model_name]["api_key"] = value
        elif field == "api_base":
            model_entries[model_name]["api_base"] = value

    # Merge into model_list (env entries take precedence, preserve existing non-env entries)
    if model_entries:
        existing_list = config.get("model_list", [])
        existing_names = {e.get("model_name") for e in existing_list if e.get("model_name")}
        new_list = [e for e in existing_list if e.get("model_name") not in model_entries]
        new_list.extend(model_entries.values())
        config["model_list"] = new_list
        logging.info("Applied %d model entries from env vars: %s", len(model_entries), list(model_entries.keys()))

    # Default model_name and model (must match a model_name in model_list)
    default_model_name = os.environ.get("PICOCLAW_DEFAULT_MODEL_NAME", "")
    if default_model_name:
        defaults = config.setdefault("agents", {}).setdefault("defaults", {})
        defaults["model_name"] = default_model_name
        # PicoClaw gateway expects 'model' to match a model_name in model_list
        # Sync them to ensure consistency
        defaults["model"] = default_model_name

    # Legacy provider overrides (kept for UI compatibility, will be transformed to model_list)
    legacy_prefix = "PICOCLAW_PROVIDER_"
    for key, value in os.environ.items():
        if not key.startswith(legacy_prefix):
            continue
        rest = key[len(legacy_prefix):]
        parts = rest.split("_", 1)
        if len(parts) != 2:
            continue
        provider_name, field_name = parts[0].lower(), parts[1].lower()

        providers = config.get("providers", {})
        if provider_name not in providers:
            providers[provider_name] = {}
            config["providers"] = providers

        if field_name == "enabled":
            parsed = value.lower() in ("true", "1", "yes")
        else:
            parsed = value

        providers[provider_name][field_name] = parsed

    # Transform legacy providers with api_key into model_list entries
    # This ensures the gateway subprocess has a valid model_list
    _transform_providers_to_model_list(config)

    # Channel overrides
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

    return config


def _transform_providers_to_model_list(config):
    """Transform legacy providers with api_key into model_list entries.

    This bridges the UI's provider-centric view with picoclaw's model_list schema.
    """
    providers = config.get("providers", {})
    model_list = config.get("model_list", [])
    existing_names = {e.get("model_name") for e in model_list if e.get("model_name")}

    for prov_name, prov_cfg in providers.items():
        if not isinstance(prov_cfg, dict):
            continue
        api_key = prov_cfg.get("api_key", "")
        if not api_key or prov_cfg.get("enabled") is False:
            continue
        if prov_name in existing_names:
            continue

        # Build model entry from provider
        model_spec = f"{prov_name}/default"  # Will need user to specify actual model
        if prov_name == "openrouter":
            model_spec = "openrouter/anthropic/claude-sonnet-4"  # Sensible default
        elif prov_name == "openai":
            model_spec = "openai/gpt-4o"
        elif prov_name == "anthropic":
            model_spec = "anthropic/claude-sonnet-4"
        elif prov_name == "deepseek":
            model_spec = "deepseek/deepseek-chat"
        elif prov_name == "gemini":
            model_spec = "gemini/gemini-2.0-flash"
        elif prov_name == "groq":
            model_spec = "groq/llama-3.3-70b-versatile"
        elif prov_name == "zhipu":
            model_spec = "zhipu/glm-4"
        elif prov_name == "moonshot":
            model_spec = "moonshot/moonshot-v1-8k"

        entry = {
            "model_name": prov_name,
            "model": model_spec,
            "api_key": api_key,
        }
        if prov_cfg.get("api_base"):
            entry["api_base"] = prov_cfg["api_base"]

        model_list.append(entry)
        logging.info("Transformed provider '%s' to model_list entry: %s", prov_name, model_spec)

    if model_list:
        config["model_list"] = model_list


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
                "model_name": "",
                "max_tokens": 8192,
                "temperature": 0.7,
                "max_tool_iterations": 20,
            }
        },
        "model_list": [],
        "channels": {
            "telegram": {"enabled": False, "token": "", "proxy": "", "allow_from": []},
            "discord": {"enabled": False, "token": "", "allow_from": []},
            "slack": {"enabled": False, "bot_token": "", "app_token": "", "allow_from": []},
            "whatsapp": {"enabled": False, "bridge_url": "ws://localhost:3001", "allow_from": []},
            "feishu": {"enabled": False, "app_id": "", "app_secret": "", "encrypt_key": "", "verification_token": "", "allow_from": []},
            "dingtalk": {"enabled": False, "client_id": "", "client_secret": "", "allow_from": []},
            "qq": {"enabled": False, "app_id": "", "app_secret": "", "allow_from": []},
            "line": {"enabled": False, "channel_secret": "", "channel_access_token": "", "webhook_path": "/webhook/line", "allow_from": []},
            "maixcam": {"enabled": False, "host": "0.0.0.0", "port": 18790, "allow_from": []},
        },
        "providers": {
            # Legacy section kept for UI compatibility; transformed to model_list for gateway
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
        "gateway": {"host": "0.0.0.0", "port": 18790, "log_level": "fatal"},
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
            
            # Log channel status before saving
            channels_enabled = [name for name, cfg in resolved.get("channels", {}).items()
                               if isinstance(cfg, dict) and cfg.get("enabled")]
            logging.info("Channels enabled in resolved config: %s", channels_enabled or "none")
            
            # Debug: log the actual discord config being written
            discord_cfg = resolved.get("channels", {}).get("discord", {})
            logging.info("Discord config being written: enabled=%s, token=%s...", 
                        discord_cfg.get("enabled"), 
                        discord_cfg.get("token", "")[:10] + "..." if discord_cfg.get("token") else "empty")
            
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
                    model_name = config.get("agents", {}).get("defaults", {}).get("model_name", "")
                    model_list = config.get("model_list", [])
                    if not model_name:
                        _append_log("  Likely cause: no model_name set (agents.defaults.model_name is empty)")
                        _append_log("  Fix: set PICOCLAW_DEFAULT_MODEL_NAME=openrouter in your environment")
                    if not model_list:
                        _append_log("  Likely cause: model_list is empty (no models configured)")
                    model_names = [e.get("model_name") for e in model_list]
                    _append_log(f"  Current config: model_name={model_name!r}, model_list={model_names}")
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

    # Build providers status from model_list (preferred) + legacy providers
    providers = {}
    for entry in config.get("model_list", []):
        name = entry.get("model_name", "unknown")
        providers[name] = {
            "enabled": True,
            "configured": bool(entry.get("api_key")),
            "model": entry.get("model", ""),
        }
    for name, prov in config.get("providers", {}).items():
        if name not in providers:
            providers[name] = {"enabled": prov.get("enabled", False), "configured": bool(prov.get("api_key"))}

    model_name = config.get("agents", {}).get("defaults", {}).get("model_name", "")
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
    model_list = config.get("model_list", [])
    model_name = config.get("agents", {}).get("defaults", {}).get("model_name", "")
    if model_list and model_name:
        model_names = [e.get("model_name") for e in model_list if e.get("model_name")]
        _append_log(f"Auto-starting gateway with model_name={model_name!r}, models: {model_names}")
        logging.info("Auto-starting gateway: model_name=%s, models=%s", model_name, model_names)
        asyncio.create_task(gateway.start())
    else:
        reasons = []
        if not model_list:
            reasons.append("model_list is empty")
        if not model_name:
            reasons.append("model_name not set")
        _append_log(f"Gateway not auto-started: {', '.join(reasons)}")
        logging.info("Skipping auto-start: %s", ", ".join(reasons))


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
