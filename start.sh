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

# Start the launcher web console (includes gateway management)
# Port 18800 is the web UI, port 18790 is the gateway
exec picoclaw-launcher
