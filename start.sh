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

# Set up HTTP Basic Auth for the web UI
# Uses AUTH_USERNAME and AUTH_PASSWORD env vars (defaults provided)
AUTH_USER="${AUTH_USERNAME:-admin}"
AUTH_PASS="${AUTH_PASSWORD:-$(openssl rand -base64 12)}"
echo "Setting up Basic Auth: user=$AUTH_USER"
echo "$AUTH_USER:$(openssl passwd -apr1 "$AUTH_PASS")" > /etc/nginx/.htpasswd

# Print password on first startup (check Railway logs to see it)
if [ ! -f /data/.picoclaw/.auth_printed ]; then
    echo "============================================"
    echo "PicoClaw Web UI Credentials:"
    echo "  Username: $AUTH_USER"
    echo "  Password: $AUTH_PASS"
    echo "============================================"
    echo "Set AUTH_USERNAME and AUTH_PASSWORD env vars to customize."
    touch /data/.picoclaw/.auth_printed
fi

# Update nginx config with the correct port
NGINX_PORT="${PORT:-8080}"
sed "s/listen 8080;/listen $NGINX_PORT;/" /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

# Kill any existing launcher process on port 18800
pkill -f "picoclaw-launcher" 2>/dev/null || true

# Wait for port to be free (handle rapid container restarts)
for i in $(seq 1 10); do
    if ! ss -tln | grep -q ":18800 "; then
        break
    fi
    echo "Waiting for port 18800 to be free... ($i/10)"
    sleep 1
done

# Start the launcher on localhost only (nginx proxies to it)
picoclaw-launcher -port 18800 &
LAUNCHER_PID=$!

# Start nginx as the public-facing proxy with basic auth
nginx -g 'daemon off;' &
NGINX_PID=$!

# Handle shutdown gracefully
trap "kill $LAUNCHER_PID $NGINX_PID 2>/dev/null; exit 0" SIGTERM SIGINT

# Wait for either process to exit
wait -n $LAUNCHER_PID $NGINX_PID
