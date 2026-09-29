#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
DIST="${ARC_DIST_DIR:-$ROOT/dist}"
STAGE="$DIST/submission"
STARTER_ZIP="${ARC_FACTORY26_STARTER_ZIP:-$ROOT/agent-claude-code-based.zip}"
MAX_BYTES="${ARC_SUBMISSION_MAX_BYTES:-52428800}"

command -v python3 >/dev/null 2>&1 || { echo "missing required command: python3" >&2; exit 2; }
[[ -f "$STARTER_ZIP" ]] || {
  echo "Factory26 Claude Code starter ZIP not found: $STARTER_ZIP" >&2
  echo "Set ARC_FACTORY26_STARTER_ZIP to the official downloaded agent-claude-code-based.zip" >&2
  exit 2
}

rm -rf "$STAGE"
mkdir -p "$STAGE"
cp "$ROOT/main.py" "$ROOT/sdk_driver.py" "$ROOT/runtime.lock.json" "$ROOT/requirements.txt" "$STAGE/"
# v5aj: harness_supervisor deleted — do not pack supervisor.py or ARC_HARNESS_SUPERVISOR sentinel.

echo "[1/2] import current ARC-Bench Factory starter assets"
python3 - "$STARTER_ZIP" "$STAGE" <<'PY'
from pathlib import Path
import sys, zipfile
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
with zipfile.ZipFile(src) as zf:
    names = {name.replace("\\", "/") for name in zf.namelist()}
    required = {
        "template/template.yaml",
        "skills/arcbench-checkpoint/SKILL.md",
        "skills/arcbench-runtime-signals/SKILL.md",
        "skills/arcbench-traceability/SKILL.md",
        "arcbench-agent-runtime/pyproject.toml",
    }
    missing = sorted(required - names)
    if missing:
        raise SystemExit("official starter ZIP is missing: " + ", ".join(missing))
    root = dst.resolve()
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        target = (dst / name).resolve()
        if root not in target.parents:
            raise SystemExit(f"unsafe path in Factory starter ZIP: {info.filename}")
        if (
            name.startswith("template/")
            or name.startswith("skills/")
            or name.startswith("arcbench-agent-runtime/")
        ):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
PY

echo "[1b/2] copy contest skills + Tao CLAUDE.md + anthropic-proxy (Messages to chat bridge)"
# Tao contest user-level CLAUDE.md (v5ah) — installed to HOME/.claude + project on every run
if [[ -f "$ROOT/contest/CLAUDE.md" ]]; then
  mkdir -p "$STAGE/contest"
  cp -f "$ROOT/contest/CLAUDE.md" "$STAGE/contest/CLAUDE.md"
  echo "  + contest/CLAUDE.md ($(wc -c < "$STAGE/contest/CLAUDE.md") bytes)"
else
  echo "ERROR: missing $ROOT/contest/CLAUDE.md (Tao contest guidance required for v5ah)" >&2
  exit 2
fi
# Architect/designer skills (not in Factory starter ZIP)
for skill in architect designer; do
  if [[ -d "$ROOT/skills/$skill" ]]; then
    mkdir -p "$STAGE/skills/$skill"
    cp -a "$ROOT/skills/$skill/." "$STAGE/skills/$skill/"
    echo "  + skills/$skill"
  fi
done
# Tao chose A (2026-09-27): protocol bridge REQUIRED for ARC deepseek (Messages↔chat/completions).
# Always pack anthropic-proxy for contest Smoke/Official (opt-out of pack only by removing the binary).
PROXY_SRC="$ROOT/runtime/gateway/anthropic-proxy"
if [[ -f "$PROXY_SRC" ]]; then
  mkdir -p "$STAGE/runtime/gateway"
  cp -f "$PROXY_SRC" "$STAGE/runtime/gateway/anthropic-proxy"
  chmod +x "$STAGE/runtime/gateway/anthropic-proxy"
  echo "  + runtime/gateway/anthropic-proxy ($(stat -c %s "$STAGE/runtime/gateway/anthropic-proxy") bytes)"
else
  echo "WARNING: missing $PROXY_SRC — Claude will hit Messages/chat protocol mismatch" >&2
fi

echo "[2/2] package slim ARC submission"
rm -f "$DIST/submission.zip"
python3 - "$STAGE" "$DIST/submission.zip" <<'PY'
from pathlib import Path
import sys, zipfile
root, out = Path(sys.argv[1]), Path(sys.argv[2])
with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            zf.write(path, path.relative_to(root))
PY

size="$(stat -c %s "$DIST/submission.zip")"
if (( size > MAX_BYTES )); then
  echo "submission.zip exceeds limit: $size > $MAX_BYTES bytes" >&2
  exit 3
fi

echo "submission directory: $STAGE"
echo "submission zip:       $DIST/submission.zip"
echo "submission bytes:     $size / $MAX_BYTES"
du -sh "$STAGE" "$DIST/submission.zip"
