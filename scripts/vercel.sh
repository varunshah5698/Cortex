#!/usr/bin/env bash
# The Vercel CLI installed into the project (Render's build cannot write /usr/lib).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/../.vercel-cli/node_modules/.bin/vercel" "$@"
