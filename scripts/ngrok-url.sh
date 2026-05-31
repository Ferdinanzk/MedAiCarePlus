#!/usr/bin/env bash
# Fetch the public ngrok URL and print the LINE webhook endpoint.

set -euo pipefail

TUNNELS=$(curl -s http://localhost:4040/api/tunnels)
PUBLIC_URL=$(echo "$TUNNELS" | python3 -m json.tool | grep '"public_url"' | head -1 | sed 's/.*"public_url": *"\(.*\)".*/\1/')

if [ -z "$PUBLIC_URL" ]; then
    echo "No public ngrok tunnel found. Is ngrok running?"
    exit 1
fi

echo "LINE Webhook URL: ${PUBLIC_URL}/line/webhook"
