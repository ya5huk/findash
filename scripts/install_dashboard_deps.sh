#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/../dashboard"
npm ci
