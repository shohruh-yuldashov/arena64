-- Runs once, only on a fresh volume (PostgreSQL's official image
-- convention for /docker-entrypoint-initdb.d/). A second, separate
-- database for tests/contract/ — never the same database `local`
-- development points at, so a test that bypasses the rollback fixture
-- (a raw connection, an explicit commit) cannot corrupt real dev data.
CREATE DATABASE arena64_test OWNER arena64;
