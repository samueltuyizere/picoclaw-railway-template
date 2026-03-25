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

# Sync channel env vars to .security.yml for Go gateway
# The Go picoclaw gateway reads tokens from .security.yml, not from config.json
sync_channel_env_to_security() {
    local security_file="$PICOCLAW_HOME/.security.yml"
    local temp_file=$(mktemp)
    
    # Create .security.yml if it doesn't exist
    if [ ! -f "$security_file" ]; then
        echo "model_list: {}" > "$security_file"
        echo "channels: {}" >> "$security_file"
        echo "web: {}" >> "$security_file"
        echo "skills: {}" >> "$security_file"
    fi
    
    # Process PICOCLAW_CHANNEL_* env vars
    for env_var in $(env | grep "^PICOCLAW_CHANNEL_" | cut -d= -f1); do
        # Extract channel name and field from env var name
        # PICOCLAW_CHANNEL_DISCORD_TOKEN -> discord, token
        local rest="${env_var#PICOCLAW_CHANNEL_}"
        local channel_name=$(echo "$rest" | cut -d_ -f1 | tr '[:upper:]' '[:lower:]')
        local field_name=$(echo "$rest" | cut -d_ -f2- | tr '[:upper:]' '[:lower:]')
        local value="${!env_var}"
        
        if [ -n "$value" ]; then
            echo "Syncing env var $env_var -> channels.$channel_name.$field_name"
            
            # Use yq or sed to update the yaml file
            if command -v yq &> /dev/null; then
                yq -i ".channels.$channel_name.$field_name = \"$value\"" "$security_file"
            else
                # Fallback: manually update using sed/awk
                # Check if channel entry exists
                if ! grep -q "^  $channel_name:" "$security_file"; then
                    # Add channel entry
                    sed -i "s/^channels:$/channels:\\n  $channel_name: {}/" "$security_file" 2>/dev/null || \
                    sed -i '' "s/^channels:$/channels:\\n  $channel_name: {}/" "$security_file"
                fi
                # For now, we'll use a Python one-liner if available
                if command -v python3 &> /dev/null; then
                    python3 -c "
import yaml
with open('$security_file', 'r') as f:
    data = yaml.safe_load(f) or {}
data.setdefault('channels', {}).setdefault('$channel_name', {})
data['channels']['$channel_name']['$field_name'] = '$value'
with open('$security_file', 'w') as f:
    yaml.dump(data, f, default_flow_style=False)
"
                fi
            fi
        fi
    done
}

# Install yq for YAML manipulation if not present
if ! command -v yq &> /dev/null && ! command -v python3 &> /dev/null; then
    curl -sL https://github.com/mikefarah/yq/releases/download/v4.35.1/yq_linux_amd64 -o /tmp/yq
    chmod +x /tmp/yq
    export PATH="$PATH:/tmp"
fi

sync_channel_env_to_security

# Use Railway's PORT if available, otherwise default to 18800
LAUNCHER_PORT="${PORT:-18800}"

# Start the launcher web console (includes gateway management)
# -public flag makes it listen on 0.0.0.0 (required for Railway)
# -port flag sets the port (Railway assigns dynamic PORT)
exec picoclaw-launcher -public -port "$LAUNCHER_PORT"
