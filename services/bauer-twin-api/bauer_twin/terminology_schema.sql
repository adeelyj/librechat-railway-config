CREATE SCHEMA IF NOT EXISTS bauer_twin;

CREATE TABLE IF NOT EXISTS bauer_twin.terminology_aliases (
    domain text NOT NULL,
    canonical_value text NOT NULL,
    alias text NOT NULL,
    language text NOT NULL,
    query_safe boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (domain, alias)
);

CREATE INDEX IF NOT EXISTS terminology_canonical_idx
    ON bauer_twin.terminology_aliases (domain, canonical_value);
