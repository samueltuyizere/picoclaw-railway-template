# PicoClaw Railway Template (1-click deploy)

This repo packages **PicoClaw** for Railway with the built-in Launcher Web UI for configuration and chat.

## What you get

- **PicoClaw Launcher** - Official web-based UI for configuration and chat (port 18800)
- **PicoClaw Gateway** - Bot gateway for Discord, Telegram, Slack, and more (port 18790)
- **Persistent state** via Railway Volume (config, workspace, sessions survive redeploys)

## How it works

- The container builds PicoClaw from source, including the launcher binary
- The launcher provides a browser-based UI at port 18800 for configuration and chat
- The launcher manages the gateway process automatically
- Configuration is stored in `/data/.picoclaw/config.json`

## Ports

| Port | Service | Description |
|------|---------|-------------|
| 18800 | Launcher | Web UI for configuration and chat |
| 18790 | Gateway | Bot communication (Discord, Telegram, etc.) |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PICOCLAW_VERSION` | `main` | Git branch/tag to build PicoClaw from |
| `PICOCLAW_HOME` | `/data/.picoclaw` | Config directory location |
| `PICOCLAW_GATEWAY_HOST` | `0.0.0.0` | Gateway listen address (set for external access) |

## Channel configuration via environment

You can configure channels directly via Railway environment variables:

```
PICOCLAW_CHANNEL_DISCORD_ENABLED=true
PICOCLAW_CHANNEL_DISCORD_TOKEN=your-bot-token
PICOCLAW_CHANNEL_TELEGRAM_ENABLED=true
PICOCLAW_CHANNEL_TELEGRAM_TOKEN=your-bot-token
```

## Model configuration via environment

```
PICOCLAW_MODEL_OPENROUTER_MODEL=openrouter/anthropic/claude-sonnet-4
PICOCLAW_MODEL_OPENROUTER_API_KEY=sk-or-v1-xxx
PICOCLAW_DEFAULT_MODEL_NAME=openrouter
```

## Getting chat tokens

### Telegram bot token

1. Open Telegram and message **@BotFather**
2. Run `/newbot` and follow the prompts
3. BotFather will give you a token like: `123456789:AA...`
4. Set `PICOCLAW_CHANNEL_TELEGRAM_TOKEN` and `PICOCLAW_CHANNEL_TELEGRAM_ENABLED=true`

### Discord bot token

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. **New Application** → pick a name
3. Open the **Bot** tab → **Add Bot**
4. Enable **MESSAGE CONTENT INTENT** under Privileged Gateway Intents
5. Copy the **Bot Token** and set `PICOCLAW_CHANNEL_DISCORD_TOKEN`
6. Set `PICOCLAW_CHANNEL_DISCORD_ENABLED=true`
7. Invite the bot to your server (OAuth2 URL Generator → scopes: `bot`, `applications.commands`)

## Local testing

```bash
docker build -t picoclaw-railway-template .

docker run --rm -p 18800:18800 -p 18790:18790 \
  -v $(pwd)/.tmpdata:/data \
  picoclaw-railway-template

# Open http://localhost:18800 for the web UI
```

## FAQ

**Q: How do I access the web UI?**

A: Go to your deployed instance's URL on port 18800. The launcher provides a full configuration and chat interface.

**Q: The gateway shows "No channels enabled". What's wrong?**

A: Make sure you've set both the channel token AND enabled flag:
- `PICOCLAW_CHANNEL_DISCORD_TOKEN=your-token`
- `PICOCLAW_CHANNEL_DISCORD_ENABLED=true`

**Q: How do I change the AI model?**

A: Either use the web UI or set environment variables:
- `PICOCLAW_DEFAULT_MODEL_NAME=openrouter`
- `PICOCLAW_MODEL_OPENROUTER_MODEL=openrouter/anthropic/claude-sonnet-4`
- `PICOCLAW_MODEL_OPENROUTER_API_KEY=sk-or-v1-xxx`
