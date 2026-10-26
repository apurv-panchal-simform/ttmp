-- Facts store (plan section 3.4), schema version 1. Applied by `tmp-pipeline-db migrate`; every statement is
-- idempotent. Every row carries doc_id and the access groups that may read it; deleting a tmp_document row removes
-- everything written for that document, so a re-write is one transaction (delete, then insert).
-- Values are typed (numbers with unit and raw text beside them in `fact`, times as time, dates as date); a value in
-- conflict is NULL in the typed tables and its versions are in `conflict`. There is no jsonb copy of the record.

CREATE TABLE IF NOT EXISTS schema_version (
    version     integer PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tmp_document (
    doc_id                text PRIMARY KEY,
    access_groups         text[] NOT NULL DEFAULT '{}',
    tmp_reference         text,
    tmp_reference_status  text,
    order_reference       text,
    approval_status       text,
    revision              integer NOT NULL,
    config_hash           text,
    catalog_version       text,
    versions              jsonb NOT NULL DEFAULT '{}',
    written_at            timestamptz NOT NULL DEFAULT now()
);

-- every value of every fact, with its status, method and review (the typed tables below are views of these values)
CREATE TABLE IF NOT EXISTS fact (
    doc_id         text NOT NULL REFERENCES tmp_document ON DELETE CASCADE,
    access_groups  text[] NOT NULL DEFAULT '{}',
    fact_id        text NOT NULL,
    entity         text NOT NULL,
    field          text NOT NULL,
    value_text     text,
    value_num      double precision,
    value_json     jsonb,
    raw            text,
    unit           text,
    status         text NOT NULL,
    method         text,
    notes          text[] NOT NULL DEFAULT '{}',
    link           text[] NOT NULL DEFAULT '{}',
    review         jsonb,
    applicability  text NOT NULL DEFAULT 'active',
    PRIMARY KEY (doc_id, fact_id, field)
);

CREATE TABLE IF NOT EXISTS field_evidence (
    doc_id         text NOT NULL REFERENCES tmp_document ON DELETE CASCADE,
    access_groups  text[] NOT NULL DEFAULT '{}',
    fact_id        text NOT NULL,
    field          text NOT NULL,
    record_id      text NOT NULL,
    page           integer NOT NULL,
    element_ids    text[] NOT NULL DEFAULT '{}',
    raw            text
);

CREATE TABLE IF NOT EXISTS road (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    fact_id text NOT NULL, road_name text, suburb text, start_rp double precision, end_rp double precision,
    onrc_class text, posted_speed_kmh integer, aadt integer, hmv_pct double precision, peak_periods text,
    PRIMARY KEY (doc_id, fact_id)
);

CREATE TABLE IF NOT EXISTS shift_window (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    fact_id text NOT NULL, day_type text, first_sign_in time, tc_in time, pick_up time, site_cleared time,
    PRIMARY KEY (doc_id, fact_id)
);

CREATE TABLE IF NOT EXISTS work_stage (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    fact_id text NOT NULL, tmd_id text, title text, hours text,
    PRIMARY KEY (doc_id, fact_id)
);

CREATE TABLE IF NOT EXISTS speed_limit (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    fact_id text NOT NULL, road_name text, speed_kmh integer, length_m double precision, rp_from double precision,
    rp_to double precision, date_from date, date_to date, tmd_ids text[],
    PRIMARY KEY (doc_id, fact_id)
);

-- the hours a speed limit applies, one row per day type
CREATE TABLE IF NOT EXISTS speed_limit_window (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    fact_id text NOT NULL, day_type text, time_from time, time_to time, raw text
);

CREATE TABLE IF NOT EXISTS authorisation (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    fact_id text NOT NULL, items text[],
    PRIMARY KEY (doc_id, fact_id)
);

CREATE TABLE IF NOT EXISTS contact (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    fact_id text NOT NULL, name text, roles text[], organisation text, phone text, email text,
    PRIMARY KEY (doc_id, fact_id)
);

CREATE TABLE IF NOT EXISTS tmd_sheet (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    tmd_id text NOT NULL, sheet text, title text, rev text, page integer, record_id text, aliases text[],
    PRIMARY KEY (doc_id, tmd_id)
);

CREATE TABLE IF NOT EXISTS tmd_mention (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    mention_id text NOT NULL, text text, number integer, title text, page integer, record_id text,
    mapped_to text, method text, status text, id_conflict boolean NOT NULL DEFAULT false,
    PRIMARY KEY (doc_id, mention_id)
);

CREATE TABLE IF NOT EXISTS tmd_symbol (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    tmd_id text NOT NULL, symbol_id text NOT NULL, kind text, value text, unit text, rp double precision,
    road text, text text,
    PRIMARY KEY (doc_id, tmd_id, symbol_id)
);

CREATE TABLE IF NOT EXISTS risk (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    fact_id text NOT NULL, category text, hazard text, groups_at_risk text, potential_event text,
    initial_likelihood text, initial_consequence text, initial_risk text, controls text, residual_likelihood text,
    residual_consequence text, residual_risk text, rating_mismatch boolean NOT NULL DEFAULT false,
    PRIMARY KEY (doc_id, fact_id)
);

CREATE TABLE IF NOT EXISTS risk_tmd (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    fact_id text NOT NULL, tmd_id text NOT NULL,
    PRIMARY KEY (doc_id, fact_id, tmd_id)
);

CREATE TABLE IF NOT EXISTS risk_matrix_cell (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    fact_id text NOT NULL, likelihood text, impact text, score integer, rating text,
    PRIMARY KEY (doc_id, fact_id)
);

CREATE TABLE IF NOT EXISTS section (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    fact_id text NOT NULL, heading text, section_type text, applicability text, pages integer[],
    PRIMARY KEY (doc_id, fact_id)
);

CREATE TABLE IF NOT EXISTS figure (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    fact_id text NOT NULL, image_kind text, description text, pages integer[], tmd_ids text[],
    PRIMARY KEY (doc_id, fact_id)
);

-- ticked boxes (vulnerable users, road aspects) and yes/no form answers
CREATE TABLE IF NOT EXISTS checkbox (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    fact_id text NOT NULL, field text NOT NULL, question text, answer text, status text,
    PRIMARY KEY (doc_id, fact_id, field)
);

CREATE TABLE IF NOT EXISTS signoff (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    role text NOT NULL, page integer, status text NOT NULL, name text, organisation text, email text, phone text,
    signature text, date_text text, notes text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS conflict (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    conflict_id text NOT NULL, rule integer NOT NULL, title text, versions jsonb NOT NULL, affected text[],
    review jsonb,
    PRIMARY KEY (doc_id, conflict_id)
);

CREATE TABLE IF NOT EXISTS check_result (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    rule integer NOT NULL, name text, outcome text, detail text, conflict_id text,
    PRIMARY KEY (doc_id, rule)
);

CREATE TABLE IF NOT EXISTS review_item (
    doc_id text NOT NULL REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    item_id text NOT NULL, kind text NOT NULL, ref text NOT NULL, reason text, pages integer[], status text NOT NULL,
    PRIMARY KEY (doc_id, item_id)
);

CREATE TABLE IF NOT EXISTS coverage (
    doc_id text PRIMARY KEY REFERENCES tmp_document ON DELETE CASCADE, access_groups text[] NOT NULL DEFAULT '{}',
    elements integer, records integer, leftover text[], records_unaccounted text[], complete boolean
);

INSERT INTO schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;
