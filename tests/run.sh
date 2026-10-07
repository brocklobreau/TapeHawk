#!/bin/sh
# Every test script, in name order; each prints its own "... ok" line last.
# Globbed rather than listed so a new test file is never left off.
cd "$(dirname "$0")"
for t in test_*.py; do python3 "$t" 2>&1 | tail -1; done
