#!/bin/sh
cd "$(dirname "$0")"
for t in test_filings13d test_gems3 test_snipe_tab test_wires; do python3 $t.py 2>&1 | tail -1; done
