#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
npm run authorize
npm run status
