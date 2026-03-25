#!/bin/bash
set -e

OBSIDIAN_DIR="/data/.picoclaw/workspace/obsidian"
REPO_URL="${OBSIDIAN_REPO_URL:-}"
SYNC_INTERVAL="${OBSIDIAN_SYNC_INTERVAL:-300}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

# Configure git
git config --global user.email "picoclaw@railway.app"
git config --global user.name "PicoClaw Bot"
git config --global init.defaultBranch main

# Clone repo if token is provided
if [ -n "$GITHUB_TOKEN" ] && [ -n "$REPO_URL" ]; then
    # Inject token into URL for auth
    AUTH_URL=$(echo "$REPO_URL" | sed "s|https://|https://${GITHUB_TOKEN}@|")
    
    if [ ! -d "$OBSIDIAN_DIR/.git" ]; then
        echo "Cloning Obsidian vault..."
        git clone "$AUTH_URL" "$OBSIDIAN_DIR" || {
            echo "Failed to clone, creating empty repo"
            mkdir -p "$OBSIDIAN_DIR"
            cd "$OBSIDIAN_DIR"
            git init
            git remote add origin "$AUTH_URL"
        }
    else
        echo "Obsidian vault already exists, updating remote..."
        cd "$OBSIDIAN_DIR"
        git remote set-url origin "$AUTH_URL"
    fi
else
    echo "Warning: GITHUB_TOKEN or OBSIDIAN_REPO_URL not set, skipping Obsidian sync"
    exit 0
fi

# Sync loop
sync_vault() {
    cd "$OBSIDIAN_DIR"
    
    # Pull latest changes
    echo "[$(date)] Pulling latest changes..."
    git fetch origin
    git merge origin/main --ff-only 2>/dev/null || {
        echo "Merge conflict or diverged branches, resetting to origin/main"
        git reset --hard origin/main
    }
    
    # Check for local changes
    if [ -n "$(git status --porcelain)" ]; then
        echo "[$(date)] Committing local changes..."
        git add -A
        git commit -m "Auto-sync from PicoClaw ($(date -Iminutes))" || true
        git push origin main || {
            echo "Push failed, pulling and retrying..."
            git pull --rebase origin main
            git push origin main
        }
        echo "[$(date)] Pushed changes to GitHub"
    else
        echo "[$(date)] No local changes to sync"
    fi
}

# Initial sync
sync_vault

# Continuous sync loop
echo "Starting Obsidian sync loop (interval: ${SYNC_INTERVAL}s)"
while true; do
    sleep "$SYNC_INTERVAL"
    sync_vault
done
