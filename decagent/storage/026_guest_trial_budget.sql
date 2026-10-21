-- Public guest trial accounting is independent from task and benchmark ledgers.
-- Workspace usage rows survive guest data cleanup so usage stays auditable.
CREATE TABLE business.guest_trial_budget_days (
 budget_date date PRIMARY KEY,
 daily_cost_limit_usd numeric(18,6) NOT NULL CHECK (daily_cost_limit_usd > 0),
 reserved_cost_usd numeric(18,6) NOT NULL DEFAULT 0 CHECK (reserved_cost_usd >= 0),
 daily_qwen_token_limit bigint NOT NULL CHECK (daily_qwen_token_limit > 0),
 qwen_tokens_reserved bigint NOT NULL DEFAULT 0 CHECK (qwen_tokens_reserved >= 0),
 fx_source_date date,
 fx_source_url text,
 fx_cny_per_usd numeric(12,6),
 fx_margin_bps integer NOT NULL DEFAULT 1000
  CHECK (fx_margin_bps >= 1000 AND fx_margin_bps < 10000),
 fx_effective_cny_per_usd numeric(12,2),
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK (
  (fx_source_date IS NULL AND fx_source_url IS NULL AND fx_cny_per_usd IS NULL
   AND fx_effective_cny_per_usd IS NULL)
  OR
  (fx_source_date IS NOT NULL AND fx_source_url IS NOT NULL
   AND fx_cny_per_usd IS NOT NULL AND fx_cny_per_usd > 0
   AND fx_effective_cny_per_usd IS NOT NULL AND fx_effective_cny_per_usd > 0)
 )
);

CREATE TABLE business.guest_trial_workspace_usage (
 budget_date date NOT NULL REFERENCES business.guest_trial_budget_days(budget_date),
 workspace_id uuid NOT NULL REFERENCES business.workspaces(id),
 task_limit integer NOT NULL CHECK (task_limit > 0),
 task_count integer NOT NULL DEFAULT 0 CHECK (task_count >= 0 AND task_count <= task_limit),
 qwen_token_limit bigint NOT NULL CHECK (qwen_token_limit > 0),
 qwen_tokens_reserved bigint NOT NULL DEFAULT 0
  CHECK (qwen_tokens_reserved >= 0 AND qwen_tokens_reserved <= qwen_token_limit),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (budget_date,workspace_id)
);

CREATE INDEX guest_trial_workspace_usage_workspace_day
 ON business.guest_trial_workspace_usage(workspace_id,budget_date DESC);

CREATE TABLE business.guest_trial_task_admissions (
 budget_date date NOT NULL,
 workspace_id uuid NOT NULL,
 task_id uuid NOT NULL,
 admitted_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (workspace_id,task_id),
 FOREIGN KEY (budget_date,workspace_id)
  REFERENCES business.guest_trial_workspace_usage(budget_date,workspace_id)
);

CREATE INDEX guest_trial_task_admissions_workspace_day
 ON business.guest_trial_task_admissions(workspace_id,budget_date DESC);

CREATE TABLE business.guest_trial_usage_reservations (
 id uuid PRIMARY KEY,
 budget_date date NOT NULL,
 workspace_id uuid NOT NULL,
 -- Deliberately no FK to app.tasks: guest cleanup may remove task data, but must
 -- not erase financial reservations or unknown usage evidence.
 task_id uuid NOT NULL,
 request_key text NOT NULL CHECK (length(request_key) BETWEEN 1 AND 255),
 provider text NOT NULL CHECK (length(provider) BETWEEN 1 AND 255),
 model text NOT NULL CHECK (length(model) BETWEEN 1 AND 255),
 cost_currency text NOT NULL CHECK (cost_currency IN ('USD','CNY')),
 reserved_cost_native numeric(20,12) NOT NULL CHECK (reserved_cost_native >= 0),
 reserved_cost_usd numeric(18,6) NOT NULL CHECK (reserved_cost_usd >= 0),
 reserved_qwen_tokens bigint NOT NULL DEFAULT 0 CHECK (reserved_qwen_tokens >= 0),
 price_basis_id text NOT NULL CHECK (length(price_basis_id) BETWEEN 1 AND 255),
 status text NOT NULL DEFAULT 'reserved'
  CHECK (status IN ('reserved','settled','unknown','not_sent')),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE (workspace_id,request_key),
 FOREIGN KEY (budget_date,workspace_id)
  REFERENCES business.guest_trial_workspace_usage(budget_date,workspace_id)
);

CREATE INDEX guest_trial_usage_reservations_workspace_day
 ON business.guest_trial_usage_reservations(workspace_id,budget_date DESC,created_at DESC);
CREATE INDEX guest_trial_usage_reservations_task
 ON business.guest_trial_usage_reservations(task_id,created_at DESC);
