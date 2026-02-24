#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:18789}"
TOKEN="${GATEWAY_TOKEN:-change-me}"
HOOK_PATH="${GATEWAY_HOOK_PATH:-inbox}"

echo "[1] Health"
curl -fsS "$BASE/health" >/dev/null

echo "[2] Authorized hook"
curl -fsS "$BASE/hooks/$HOOK_PATH" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"io_id":"net0","task_id":"1","note":"smoke event","task_description":"smoke test"}' >/dev/null

echo "[3] Events"
curl -fsS "$BASE/api/events"
echo
echo "AdminAgent smoke test completed."
