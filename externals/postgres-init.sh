#!/bin/sh
# Runs once when the bank-db PostgreSQL container is first created.
# Stand-in for the bank's Oracle DB: one table, keyed by the last 4 digits of the account
# (the full number never reaches the database in this POC).
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE TABLE accounts (account_ref char(4) PRIMARY KEY, balance numeric(12,2) NOT NULL);
INSERT INTO accounts SELECT lpad(i::text, 4, '0'), round((random() * 9000 + 10)::numeric, 2) FROM generate_series(0, 9999) i;
CREATE ROLE bank_ro LOGIN PASSWORD '$BANK_RO_PASSWORD';
GRANT SELECT ON accounts TO bank_ro;
SQL
