CREATE SCHEMA IF NOT EXISTS business;

CREATE TABLE business.people (
    id uuid PRIMARY KEY,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    team text NOT NULL CHECK (length(team) BETWEEN 1 AND 200),
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE business.categories (
    id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 200)
);
CREATE TABLE business.tickets (
    id uuid PRIMARY KEY,
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    description text NOT NULL DEFAULT '',
    category_id uuid NOT NULL REFERENCES business.categories(id)
        DEFERRABLE INITIALLY DEFERRED,
    requester_id uuid REFERENCES business.people(id) DEFERRABLE INITIALLY DEFERRED,
    assignee_id uuid REFERENCES business.people(id) DEFERRABLE INITIALLY DEFERRED,
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','assigned','in_progress','resolved','closed')),
    priority text NOT NULL DEFAULT 'normal' CHECK (priority IN ('normal','high','urgent')),
    resolution text,
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status NOT IN ('assigned','in_progress') OR assignee_id IS NOT NULL),
    CHECK (status NOT IN ('resolved','closed') OR length(trim(resolution)) > 0)
);
CREATE INDEX tickets_status_idx ON business.tickets(status,updated_at);
CREATE TABLE business.ticket_history (
    id uuid PRIMARY KEY,
    ticket_id uuid NOT NULL REFERENCES business.tickets(id) DEFERRABLE INITIALLY DEFERRED,
    revision integer NOT NULL CHECK (revision > 0),
    command text NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(ticket_id,revision)
);
CREATE TABLE business.operation_receipts (
    operation_id text PRIMARY KEY CHECK (length(operation_id) BETWEEN 1 AND 200),
    input_hash text NOT NULL CHECK (input_hash ~ '^[a-f0-9]{64}$'),
    command text NOT NULL,
    resource_id uuid REFERENCES business.tickets(id) DEFERRABLE INITIALLY DEFERRED,
    revision integer,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
