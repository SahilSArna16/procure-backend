CREATE SCHEMA IF NOT EXISTS procurement;
CREATE TABLE IF NOT EXISTS procurement.workspaces (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token uuid NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON SCHEMA procurement FROM PUBLIC;
REVOKE ALL ON procurement.workspaces FROM PUBLIC;
