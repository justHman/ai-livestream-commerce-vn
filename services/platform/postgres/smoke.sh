#!/usr/bin/env sh
set -eu

: "${DATABASE_URL:?DATABASE_URL is required}"
python -c 'import os; import psycopg; psycopg.connect(os.environ["DATABASE_URL"]).close(); print("postgres ok")'
