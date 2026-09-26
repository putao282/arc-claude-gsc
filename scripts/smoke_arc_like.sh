#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
DIST="${ARC_DIST_DIR:-$ROOT/dist}"
IMAGE="${ARC_SMOKE_IMAGE:-mcr.microsoft.com/playwright/python:v1.54.0-jammy}"
CPUS="${ARC_SMOKE_CPUS:-4}"
MEMORY="${ARC_SMOKE_MEMORY:-8g}"
WORK="$DIST/arc-smoke-workspace"
STARTER="${ARC_FACTORY26_STARTER_ZIP:-$DIST/factory26-smoke-starter.zip}"
MAX_BYTES="${ARC_SUBMISSION_MAX_BYTES:-52428800}"

if [[ "${ARC_SKIP_PACKAGE:-0}" != "1" ]]; then
  if [[ ! -f "$STARTER" ]]; then
    echo "[smoke] downloading current ARC-Bench Claude Code starter"
    python3 - "$STARTER" <<'PY'
from pathlib import Path
import sys, urllib.request
path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
url = "https://www.arc-bench.com/api/competitions/smoke/starter-agent?language=python&template=claude_code"
req = urllib.request.Request(url, headers={"User-Agent": "arc-claude-gsc-smoke"})
with urllib.request.urlopen(req, timeout=120) as response:
    path.write_bytes(response.read())
PY
  fi
  ARC_FACTORY26_STARTER_ZIP="$STARTER" "$ROOT/scripts/package_submission.sh"
fi

test -f "$DIST/submission.zip"
size="$(stat -c %s "$DIST/submission.zip")"
(( size <= MAX_BYTES )) || { echo "submission.zip exceeds upload limit: $size bytes" >&2; exit 1; }

python3 - "$DIST/submission.zip" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as zf:
    names = set(zf.namelist())
    required = {
        "main.py",
        "requirements.txt",
        "runtime.lock.json",
        "arcbench-agent-runtime/pyproject.toml",
        "skills/arcbench-checkpoint/SKILL.md",
        "skills/arcbench-runtime-signals/SKILL.md",
        "skills/arcbench-traceability/SKILL.md",
    }
    missing = sorted(required - names)
    if missing:
        raise SystemExit("missing packaged files: " + ", ".join(missing))
    forbidden = [
        name for name in names
        if name.startswith("runtime/payloads/")
        or name.startswith("runtime/gateway/")
        or name == "runtime/bin/zstd"
    ]
    if forbidden:
        raise SystemExit("slim submission unexpectedly bundles heavy runtime files: " + ", ".join(forbidden))
PY

if [[ -d "$WORK" ]]; then
  docker run --rm -v "$DIST:/dist" "$IMAGE" bash -lc 'rm -rf /dist/arc-smoke-workspace'
fi
mkdir -p "$WORK"/{submission,requirements,output,tests,artifacts}

# Minimal frontend test tree so harness validation (anti-false-green) can pass in smoke.
# Production runs still require Claude to create real tests; smoke only proves the gate path.
mkdir -p "$WORK/output/frontend/tests"
cat > "$WORK/output/frontend/package.json" <<'PKG'
{
  "name": "arc-smoke-frontend",
  "private": true,
  "scripts": {
    "test": "node ./tests/smoke.test.js"
  }
}
PKG
cat > "$WORK/output/frontend/tests/smoke.test.js" <<'JS'
const fs = require("fs");
const path = require("path");
const marker = path.resolve(__dirname, "..", "..", "ARC_SMOKE.txt");
if (!fs.existsSync(marker)) {
  console.error("ARC_SMOKE.txt missing; Claude module work not present yet");
  process.exit(1);
}
const body = fs.readFileSync(marker, "utf8").trim();
if (body !== "ARC_CLAUDE_GSC_OK") {
  console.error("unexpected ARC_SMOKE.txt contents:", body);
  process.exit(1);
}
console.log("smoke harness validation ok");
process.exit(0);
JS

python3 - "$DIST/submission.zip" "$WORK/submission" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as zf:
    zf.extractall(sys.argv[2])
PY

cp "$ROOT/tests/arc_like/mock_openai.py" \
   "$ROOT/tests/arc_like/verify_arcbench_runtime.py" \
   "$ROOT/tests/arc_like/arcbench_runtime_sha256.json" \
   "$ROOT/tests/arc_like/test_self_heal.py" \
   "$ROOT/tests/arc_like/test_mcp_restore.py" \
   "$ROOT/tests/arc_like/test_false_green_gate.py" \
   "$WORK/tests/"

# The production slim agent talks directly to ARC-Bench's Anthropic-compatible
# endpoint. For local smoke only, anthropic-proxy provides that endpoint in
# front of the existing deterministic OpenAI mock. It is NOT bundled.
mapfile -t gateway_info < <(python3 - "$ROOT/runtime.lock.json" <<'PY'
import json, sys
j=json.load(open(sys.argv[1]))["gateway"]
print(j["version"])
print(j["asset"])
print(j["archiveSha256"])
print(j["binarySha256"])
PY
)
gateway_version="${gateway_info[0]}"
gateway_asset="${gateway_info[1]}"
gateway_archive_sha="${gateway_info[2]}"
gateway_binary_sha="${gateway_info[3]}"
gateway_archive="$WORK/tests/$gateway_asset"
curl -fL --retry 3 --retry-delay 2 \
  "https://github.com/thomas-illiet/anthropic-proxy/releases/download/$gateway_version/$gateway_asset" \
  -o "$gateway_archive"
echo "$gateway_archive_sha  $gateway_archive" | sha256sum -c -
mkdir -p "$WORK/tests/gateway"
tar -xzf "$gateway_archive" -C "$WORK/tests/gateway"
gateway_bin="$(find "$WORK/tests/gateway" -type f -name anthropic-proxy -perm -u+x | head -1)"
test -n "$gateway_bin"
echo "$gateway_binary_sha  $gateway_bin" | sha256sum -c -
cp "$gateway_bin" "$WORK/tests/anthropic-proxy"
chmod +x "$WORK/tests/anthropic-proxy"

cat > "$WORK/requirements/requirements.yaml" <<'YAML'
id: ROOT
name: Smoke root
children:
  - id: REQ-SMOKE-A
    name: First smoke requirement
    description: Create ARC_SMOKE.txt in the project root containing exactly ARC_CLAUDE_GSC_OK.
  - id: REQ-SMOKE-B
    name: Second smoke requirement
    description: Preserve ARC_SMOKE.txt and validate that previous module work remains available.
YAML

set +e
docker run --rm --cpus="$CPUS" --memory="$MEMORY"   -v "$WORK:/workspace" -w /workspace/output "$IMAGE" bash -lc '
set -e
cd /workspace/submission
python3 -m pip install -q -r requirements.txt
cd /workspace/output
python3 /workspace/tests/verify_arcbench_runtime.py
python3 /workspace/tests/test_self_heal.py
python3 /workspace/tests/test_mcp_restore.py
python3 /workspace/tests/test_false_green_gate.py

ARC_MOCK_REQUIRE_RECOVERY=1 python3 /workspace/tests/mock_openai.py >/workspace/artifacts/mock.log 2>&1 &
mockpid=$!

env   ANTHROPIC_PROXY_LISTEN_ADDR=127.0.0.1:8787   ANTHROPIC_PROXY_UPSTREAM_URL=http://127.0.0.1:19091/v1/chat/completions   ANTHROPIC_PROXY_UPSTREAM_API_KEY=mock-key   ANTHROPIC_PROXY_DEFAULT_MODEL=mock-model   ANTHROPIC_PROXY_FORCE_MODEL=1   ANTHROPIC_PROXY_TOOL_FORMAT=native   ANTHROPIC_PROXY_CLIENT_KEY=arc-local   /workspace/tests/anthropic-proxy serve >/workspace/artifacts/proxy.log 2>&1 &
proxypid=$!

sleep 1
set +e
env   OPENAI_BASE_URL=http://127.0.0.1:8787   OPENAI_API_KEY=arc-local   MODEL=mock-model   ARCBENCH_SUBMISSION_DIR=/workspace/submission   ARCBENCH_ARTIFACTS_DIR=/workspace/artifacts   ARC_RETRY_BASE_SECONDS=1   ARC_RETRY_MAX_SECONDS=2   python3 /workspace/submission/main.py /workspace/requirements     --output-dir /workspace/output --type web     >/workspace/artifacts/main.out 2>/workspace/artifacts/main.err
rc=$?
kill "$mockpid" "$proxypid" 2>/dev/null || true
exit "$rc"
'
rc=$?
set -e

if [[ "$rc" -ne 0 ]] || ! grep -Fxq ARC_CLAUDE_GSC_OK "$WORK/output/ARC_SMOKE.txt" 2>/dev/null; then
  echo "Factory26 slim smoke FAILED (main rc=$rc)" >&2
  for f in main.err main.out proxy.log mock.log; do
    echo "--- $f ---" >&2
    tail -160 "$WORK/artifacts/$f" >&2 2>/dev/null || true
  done
  exit 1
fi

require_file() {
  [[ -f "$1" ]] || { echo "smoke assertion failed: missing file $1" >&2; exit 1; }
}
require_grep() {
  local pattern="$1" file="$2" label="$3"
  grep -q -- "$pattern" "$file" || {
    echo "smoke assertion failed: $label" >&2
    echo "--- tail $file ---" >&2
    tail -120 "$file" >&2 2>/dev/null || true
    exit 1
  }
}

require_file "$WORK/output/.arc/runner-events.jsonl"
require_file "$WORK/output/.arc/traceability/requirements.json"
require_file "$WORK/output/SPEC/arcbench/REQ-SMOKE-A.md"
require_file "$WORK/output/SPEC/arcbench/REQ-SMOKE-B.md"
require_grep 'REQ-SMOKE-A' "$WORK/output/.arc/traceability/requirements.json" "REQ-SMOKE-A traceability missing"
require_grep 'REQ-SMOKE-B' "$WORK/output/.arc/traceability/requirements.json" "REQ-SMOKE-B traceability missing"
require_grep '"state": "completed"' "$WORK/output/.arc/runner-events.jsonl" "runner did not complete"
require_grep 'claude-agent-sdk 0.2.159 bundled Claude Code' "$WORK/artifacts/main.out" "expected bundled Claude Code not used"
require_grep '"event": "runtime_download"' "$WORK/artifacts/main.out" "runtime download event missing"
require_grep '"label": "GSC runtime"' "$WORK/artifacts/main.out" "GSC runtime download event missing"
require_grep '"event": "module_retry"' "$WORK/artifacts/main.out" "module retry was not observed"
require_grep 'connection reset' "$WORK/artifacts/mock.log" "fault injection did not occur"
require_grep 'recovery prompt observed' "$WORK/artifacts/mock.log" "recovery attempt did not reach mock"
require_grep '"state": "paused"' "$WORK/output/.arc/runner-events.jsonl" "runner pause event missing"
require_grep '"state": "resumed"' "$WORK/output/.arc/runner-events.jsonl" "runner resume event missing"
require_grep '"event": "module_validation"' "$WORK/artifacts/main.out" "module_validation event missing"
require_file "$WORK/output/.arc/validation/REQ-SMOKE-A.ok"
require_file "$WORK/output/.arc/validation/REQ-SMOKE-B.ok"

commit_count="$(git -c safe.directory="$WORK/output" -C "$WORK/output" rev-list --count HEAD)"
[[ "$commit_count" -ge 3 ]] || {
  echo "expected at least init + two module commits, got $commit_count" >&2
  exit 1
}

git -c safe.directory="$WORK/output" -C "$WORK/output" log --oneline -5
echo "Factory26 slim smoke PASS"
echo "submission bytes: $size"
echo "result: $(cat "$WORK/output/ARC_SMOKE.txt")"
