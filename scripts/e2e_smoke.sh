#!/usr/bin/env bash
set -euo pipefail
API="${API_BASE:-http://127.0.0.1:8000}"
TOKEN="${ADMIN_TOKEN:-dev-admin-token}"

echo "health"
curl -sf "$API/health" | tee /tmp/evo_health.json
echo

echo "capabilities"
curl -sf "$API/capabilities" | tee /tmp/evo_caps.json >/dev/null
echo

echo "agent-runner once (requires API up)"
uv run agent-runner once --api-base "$API"

echo "memory"
curl -sf -H "Authorization: Bearer $TOKEN" "$API/admin/memory" | tee /tmp/evo_memory.json
echo
echo "E2E smoke OK"