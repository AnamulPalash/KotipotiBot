#!/bin/sh
# =============================================================================
# entrypoint.sh — Patch FreqUI bundle before Nginx starts
# =============================================================================
# Fetches the FreqUI JS bundle from the freqtrade container, replaces
# hardcoded welcome strings, and writes the patched file to a local directory
# that Nginx serves statically. All other requests proxy to freqtrade:8080.
# =============================================================================

set -e

PATCH_DIR="/patched"
FREQTRADE_BASE="http://freqtrade:8080"
MAX_WAIT=60

echo "[entrypoint] Waiting for freqtrade to be ready..."
i=0
until wget -qO- "$FREQTRADE_BASE/api/v1/ping" >/dev/null 2>&1; do
    i=$((i+1))
    if [ "$i" -ge "$MAX_WAIT" ]; then
        echo "[entrypoint] Freqtrade not ready after ${MAX_WAIT}s — starting Nginx without patch"
        exec nginx -g "daemon off;"
    fi
    sleep 2
done
echo "[entrypoint] Freqtrade is up."

mkdir -p "$PATCH_DIR"

# ---- Find the JS bundle filename ----
# FreqUI serves an index.html that references the hashed bundle, e.g. /assets/index-abc123.js
echo "[entrypoint] Fetching index.html to find bundle filename..."
INDEX_HTML=$(wget -qO- "$FREQTRADE_BASE/")

# Extract the main JS bundle path (matches /assets/index-HASH.js)
BUNDLE_PATH=$(echo "$INDEX_HTML" | grep -oE '/assets/index-[^"]+\.js' | head -1)

if [ -z "$BUNDLE_PATH" ]; then
    echo "[entrypoint] Could not find JS bundle path — starting Nginx without patch"
    exec nginx -g "daemon off;"
fi

echo "[entrypoint] Found bundle: $BUNDLE_PATH"
BUNDLE_FILENAME=$(basename "$BUNDLE_PATH")

# ---- Download the bundle ----
echo "[entrypoint] Downloading bundle..."
wget -qO "$PATCH_DIR/$BUNDLE_FILENAME" "$FREQTRADE_BASE$BUNDLE_PATH"

# ---- Patch the strings ----
echo "[entrypoint] Patching welcome strings..."

# "Welcome to the Freqtrade UI" → "Welcome to KotipotiBot"
sed -i 's/Welcome to the Freqtrade UI/Welcome to KotipotiBot/g' "$PATCH_DIR/$BUNDLE_FILENAME"

# "Welcome to FreqUI" variant
sed -i 's/Welcome to FreqUI/Welcome to KotipotiBot/g' "$PATCH_DIR/$BUNDLE_FILENAME"

# Remove the "Have fun" line — replace with empty string
sed -i 's/Have fun - wishes you the Freqtrade team[^"]*//g' "$PATCH_DIR/$BUNDLE_FILENAME"
sed -i 's/Have fun - The Freqtrade Team[^"]*//g' "$PATCH_DIR/$BUNDLE_FILENAME"
sed -i 's/Have fun[^"]*Freqtrade team[^"]*//g' "$PATCH_DIR/$BUNDLE_FILENAME"

# Also patch the page title inside the bundle
sed -i 's/freqUI - KotipotiBot/KotipotiBot/g' "$PATCH_DIR/$BUNDLE_FILENAME"
sed -i 's/FreqUI/KotipotiBot/g' "$PATCH_DIR/$BUNDLE_FILENAME"
sed -i 's/freqUI/KotipotiBot/g' "$PATCH_DIR/$BUNDLE_FILENAME"
sed -i 's/Freqtrade UI/KotipotiBot/g' "$PATCH_DIR/$BUNDLE_FILENAME"

# Write the bundle path so Nginx config can reference it
echo "$BUNDLE_PATH" > "$PATCH_DIR/bundle_path.txt"

echo "[entrypoint] Patch complete: $BUNDLE_FILENAME"
echo "[entrypoint] Starting Nginx..."
exec nginx -g "daemon off;"
