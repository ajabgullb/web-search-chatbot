-- The database login Grafana's usage panels connect as (ADR-0016).
--
-- Column-level grants, not table-level: the dashboard may count messages,
-- runs, and documents, but this role cannot read what anyone wrote, a
-- password hash, a session, or a line of an uploaded document. A panel query
-- that tries fails with "permission denied" instead of quietly showing it.
--
-- Idempotent, and it starts from nothing each time: re-running after a
-- column is removed from this list takes that column's grant away too.
-- The login password is set separately (see compose.yaml), so this file
-- holds no secret and the test suite can apply it as-is.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_reader') THEN
        CREATE ROLE grafana_reader NOLOGIN;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO grafana_reader', current_database());
END
$$;

-- A slow or careless panel must not hold locks or tie up the database the
-- product runs on.
ALTER ROLE grafana_reader SET default_transaction_read_only = on;
ALTER ROLE grafana_reader SET statement_timeout = '5s';
ALTER ROLE grafana_reader CONNECTION LIMIT 4;

GRANT USAGE ON SCHEMA public TO grafana_reader;
-- Revoking a table privilege also revokes its column privileges.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_reader;

GRANT SELECT (id, created_at, is_active) ON users TO grafana_reader;
GRANT SELECT (id, user_id, created_at, deleted_at) ON conversations TO grafana_reader;
GRANT SELECT (id, conversation_id, role, status, degraded, created_at) ON messages TO grafana_reader;
GRANT SELECT (
    id, user_id, conversation_id, status, error_code, model, search_count,
    prompt_tokens, completion_tokens, started_at, completed_at, created_at
) ON runs TO grafana_reader;
GRANT SELECT (
    id, user_id, status, error_code, byte_size, page_count, created_at, updated_at
) ON documents TO grafana_reader;
GRANT SELECT (user_id, purpose, tokens, created_at) ON embedding_usage TO grafana_reader;
