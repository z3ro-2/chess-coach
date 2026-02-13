#!/usr/bin/env sh
set -eu

PROMPTS_DIR="/app/prompts"
PROMPTS_DEFAULT_DIR="/app/prompts_default"

if [ ! -d "$PROMPTS_DIR" ] || [ -z "$(ls -A "$PROMPTS_DIR" 2>/dev/null)" ]; then
  echo "Seeding default prompts into host bind mount"
  mkdir -p "$PROMPTS_DIR"
  cp -R "$PROMPTS_DEFAULT_DIR"/* "$PROMPTS_DIR"/
fi

exec "$@"
