#!/bin/bash
set -e

mkdir -p /data/.picoclaw/workspace
mkdir -p /data/.picoclaw/sessions
mkdir -p /data/.picoclaw/cron

# Initialize config if not present
if [ ! -f /data/.picoclaw/config.json ]; then
    picoclaw onboard
fi

export PICOCLAW_HOME=/data/.picoclaw
export PICOCLAW_GATEWAY_HOST=0.0.0.0

# Use Railway's PORT if available, otherwise default to 18800
LAUNCHER_PORT="${PORT:-18800}"

# Start the launcher web console (includes gateway management)
# -public flag makes it listen on 0.0.0.0 (required for Railway)
# -port flag sets the port (Railway assigns dynamic PORT)
exec picoclaw-launcher -public -port "$LAUNCHER_PORT"
