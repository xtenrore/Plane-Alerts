BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
INSERT INTO schema_migrations(version) VALUES(1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS users(id text PRIMARY KEY, role text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO users(id,role) VALUES('owner','OWNER') ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS sessions(id text PRIMARY KEY, user_id text NOT NULL REFERENCES users(id), token_hash text NOT NULL UNIQUE, csrf_hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL, revoked_at timestamptz);
CREATE INDEX IF NOT EXISTS sessions_token_active_idx ON sessions(token_hash,expires_at) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS login_attempts(id bigserial PRIMARY KEY, ip_hash text NOT NULL, succeeded boolean NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS login_attempts_recent_idx ON login_attempts(ip_hash,created_at DESC);

CREATE TABLE IF NOT EXISTS goals(id text PRIMARY KEY, title text NOT NULL, target_cents bigint NOT NULL CHECK(target_cents>=0), verified_cents bigint NOT NULL DEFAULT 0 CHECK(verified_cents>=0), status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
INSERT INTO goals(id,title,target_cents,status) VALUES('first-dollar','Earn first independently verified $1',100,'ACTIVE') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS opportunities(id text PRIMARY KEY, title text NOT NULL, description text NOT NULL DEFAULT '', source_url text, source_kind text NOT NULL DEFAULT 'REAL', platform text NOT NULL, state text NOT NULL, potential_reward_cents bigint, currency text NOT NULL DEFAULT 'USD', requires_kyc boolean, requires_card boolean, requires_upfront_payment boolean, expected_value_cents bigint, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS opportunity_evidence(id text PRIMARY KEY, opportunity_id text NOT NULL REFERENCES opportunities(id), source_url text NOT NULL, excerpt text NOT NULL, captured_at timestamptz NOT NULL DEFAULT now(), content_hash text NOT NULL);
CREATE TABLE IF NOT EXISTS council_reviews(id text PRIMARY KEY, opportunity_id text NOT NULL REFERENCES opportunities(id), role text NOT NULL, provider text, model text, structured_output jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS platform_registry(id text PRIMARY KEY, name text NOT NULL UNIQUE, status text NOT NULL, terms_url text, owner_approved_at timestamptz, metadata jsonb NOT NULL DEFAULT '{}'::jsonb, updated_at timestamptz NOT NULL DEFAULT now());
INSERT INTO platform_registry(id,name,status,metadata) VALUES('github','GitHub','RESEARCH_ONLY','{"public_research":true,"account_creation":false,"submission":"OWNER_APPROVAL"}') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS jobs(id text PRIMARY KEY, type text NOT NULL, opportunity_id text REFERENCES opportunities(id), created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, finished_at timestamptz, attempt integer NOT NULL DEFAULT 0, provider text, model text, quota_pool text, input_reference text, output_reference text, status text NOT NULL, failure_reason text, checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb, locked_by text, locked_until timestamptz);
CREATE INDEX IF NOT EXISTS jobs_queue_idx ON jobs(status,created_at);
CREATE TABLE IF NOT EXISTS job_attempts(id bigserial PRIMARY KEY, job_id text NOT NULL REFERENCES jobs(id), attempt integer NOT NULL, started_at timestamptz NOT NULL, finished_at timestamptz, status text NOT NULL, failure_reason text, checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb);

CREATE TABLE IF NOT EXISTS requests(id text PRIMARY KEY, category text NOT NULL, title text NOT NULL, description text NOT NULL, opportunity_id text REFERENCES opportunities(id), blocking_job_id text REFERENCES jobs(id), potential_reward_cents bigint, service text, capability text, requested_permission text, estimated_cost_cents bigint NOT NULL DEFAULT 0, credit_card_required text NOT NULL DEFAULT 'UNKNOWN', kyc_required text NOT NULL DEFAULT 'UNKNOWN', after_approval text NOT NULL, secret_name text, configured boolean NOT NULL DEFAULT false, verified boolean NOT NULL DEFAULT false, configured_at timestamptz, status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS request_messages(id text PRIMARY KEY, request_id text NOT NULL REFERENCES requests(id), author text NOT NULL, kind text NOT NULL CHECK(kind='MESSAGE'), body text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS provider_pools(id text PRIMARY KEY, provider text NOT NULL, credential_slot text NOT NULL UNIQUE, quota_pool text NOT NULL, account_identifier_hash text, enabled boolean NOT NULL DEFAULT false, authorized boolean NOT NULL DEFAULT false, health text NOT NULL DEFAULT 'UNCONFIGURED', free_only boolean NOT NULL DEFAULT true, last_success timestamptz, last_failure timestamptz, rate_limited_until timestamptz, quota_status text NOT NULL DEFAULT 'UNKNOWN', model text, metadata jsonb NOT NULL DEFAULT '{}'::jsonb);
CREATE TABLE IF NOT EXISTS provider_usage(id bigserial PRIMARY KEY, provider text NOT NULL, credential_slot text NOT NULL, quota_pool text NOT NULL, model text NOT NULL, input_tokens bigint NOT NULL DEFAULT 0, output_tokens bigint NOT NULL DEFAULT 0, spend_cents bigint NOT NULL DEFAULT 0 CHECK(spend_cents=0), success boolean NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS provider_quota(id text PRIMARY KEY, provider text NOT NULL, pool text NOT NULL, constraint_name text NOT NULL, quota_limit bigint, remaining bigint, reset_at timestamptz, source text NOT NULL, observed_at timestamptz NOT NULL DEFAULT now(), UNIQUE(provider,pool,constraint_name));

CREATE TABLE IF NOT EXISTS policies(id text PRIMARY KEY, version integer NOT NULL, document jsonb NOT NULL, active boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS capabilities(id text PRIMARY KEY, status text NOT NULL, configured boolean NOT NULL DEFAULT false, verified boolean NOT NULL DEFAULT false, metadata jsonb NOT NULL DEFAULT '{}'::jsonb, updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS approvals(id text PRIMARY KEY, kind text NOT NULL, entity_type text NOT NULL, entity_id text NOT NULL, decision text NOT NULL, actor text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS actions(id text PRIMARY KEY, kind text NOT NULL, status text NOT NULL, approval_id text REFERENCES approvals(id), details jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS revenue_events(id text PRIMARY KEY, opportunity_id text REFERENCES opportunities(id), state text NOT NULL, amount_cents bigint NOT NULL CHECK(amount_cents>=0), currency text NOT NULL DEFAULT 'USD', evidence_reference text, independent_verification_reference text, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS expenses(id text PRIMARY KEY, category text NOT NULL, amount_cents bigint NOT NULL CHECK(amount_cents>=0), currency text NOT NULL DEFAULT 'USD', approval_id text REFERENCES approvals(id), created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ledger_entries(id text PRIMARY KEY, entry_type text NOT NULL, amount_cents bigint NOT NULL, currency text NOT NULL DEFAULT 'USD', reference_type text NOT NULL, reference_id text NOT NULL, correction_of text REFERENCES ledger_entries(id), memo text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS wallet_accounts(id text PRIMARY KEY, network text NOT NULL, public_address text NOT NULL, status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS audit_events(id bigserial PRIMARY KEY, actor text NOT NULL, event_type text NOT NULL, entity_type text NOT NULL, entity_id text, details jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS system_checkpoints(component text PRIMARY KEY, status text NOT NULL, checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb, updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS system_state(id text PRIMARY KEY, paused boolean NOT NULL DEFAULT false, emergency_stop boolean NOT NULL DEFAULT false, updated_at timestamptz NOT NULL DEFAULT now());
INSERT INTO system_state(id) VALUES('global') ON CONFLICT DO NOTHING;

INSERT INTO policies(id,version,document) VALUES('zero-paid-ai-v1',1,'{"paid_usage":false,"maximum_spend_usd":0,"auto_upgrade":false,"paid_fallback":false,"on_exhausted":"QUEUE","unknown_provider":false}') ON CONFLICT DO NOTHING;
INSERT INTO capabilities(id,status) VALUES
  ('github.read_public_repo','AVAILABLE'),('github.prepare_patch','AVAILABLE'),('github.open_pr','APPROVAL_REQUIRED'),
  ('railway.inspect_akmp','AVAILABLE'),('railway.deploy_akmp','AVAILABLE'),('gemini.generate','UNAVAILABLE'),
  ('groq.generate','UNAVAILABLE'),('mistral.generate','UNAVAILABLE'),('cloudflare.generate','UNAVAILABLE'),
  ('wallet.receive','APPROVAL_REQUIRED'),('wallet.send','BLOCKED'),('email.send','UNAVAILABLE')
ON CONFLICT DO NOTHING;

INSERT INTO requests(id,category,title,description,service,capability,requested_permission,after_approval,status,secret_name) VALUES
  ('REQ-ADMIN-SETUP','MANUAL_ACTION','Configure A.K.M.P admin password hash','Generate an Argon2id hash locally from an owner-chosen password and set only AKMP_ADMIN_PASSWORD_HASH on akmp-control. Do not provide the plaintext password.','A.K.M.P control','admin.login','Set one server-only password hash','Owner login and all authenticated management controls become available.','WAITING_FOR_OWNER','AKMP_ADMIN_PASSWORD_HASH'),
  ('REQ-GOOGLE-AI-PRO','INFORMATION','Classify Google AI Pro access','Confirm the access source, API semantics, independent quota pool and zero-cost API terms. Consumer subscription access is not treated as Gemini API quota.','Google AI Pro','google_ai_pro.classify','Read-only classification details','If verified as a free API route, the owner may separately approve provider/model policy.','WAITING_FOR_OWNER',NULL),
  ('REQ-PROVIDER-MIGRATION','SECRET','Configure approved provider credentials','Transfer only the owner-approved Gemini, Groq, Mistral and Cloudflare credential slots using a source-side no-logging workflow or secure entry.','Approved AI providers','provider.generate','Set server-only A.K.M.P worker variables','Each credential is metadata-probed, grouped by quota ownership when known, and only then enabled.','WAITING_FOR_OWNER',NULL),
  ('REQ-SECRET-SINK','PERMISSION','Enable secure Railway secret ingestion','Provide an A.K.M.P-only, minimum-scope Railway project credential if runtime secure entry is required. Never provide a Plane Alerts token.','Railway A.K.M.P project','railway.set_akmp_worker_variable','Project-scoped variable write for A.K.M.P worker only','Secure request entry can write the value directly to the worker environment without database persistence.','WAITING_FOR_OWNER','AKMP_RAILWAY_PROJECT_TOKEN')
ON CONFLICT DO NOTHING;

INSERT INTO jobs(id,type,status,checkpoint) VALUES('JOB-FIRST-DISCOVERY','DISCOVERY_CYCLE','QUEUED','{"bounded":true,"max_results":10,"source":"github_public_search"}') ON CONFLICT DO NOTHING;

COMMIT;
