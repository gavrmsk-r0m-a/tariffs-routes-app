PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role_key TEXT NOT NULL DEFAULT 'operator',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS countries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    code TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS currencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    symbol TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS currency_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    currency_id INTEGER NOT NULL REFERENCES currencies(id) ON DELETE RESTRICT,
    rate_to_eur NUMERIC NOT NULL CHECK (rate_to_eur > 0),
    rate_date TEXT NOT NULL,
    updated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    comment TEXT,
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'api', 'import')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    provider_type TEXT NOT NULL DEFAULT 'unknown' CHECK (provider_type IN ('voip', 'sms', 'gateway', 'other', 'unknown')),
    default_currency_id INTEGER REFERENCES currencies(id) ON DELETE RESTRICT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS provider_prefixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    prefix TEXT,
    name TEXT,
    comment TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_provider_prefixes_provider_prefix
    ON provider_prefixes(provider_id, COALESCE(prefix, ''));

CREATE TABLE IF NOT EXISTS routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_id INTEGER NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    provider_prefix_id INTEGER REFERENCES provider_prefixes(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    project_label TEXT,
    cli_source_type TEXT NOT NULL CHECK (cli_source_type IN ('rnd', 'pool', 'sim', 'single_number', 'other')),
    cli_source_label TEXT NOT NULL,
    comment TEXT,
    is_actual INTEGER NOT NULL DEFAULT 1 CHECK (is_actual IN (0, 1)),
    priority_status TEXT NOT NULL DEFAULT 'unknown' CHECK (priority_status IN ('priority', 'alternative', 'normal', 'unknown')),
    inbound_line_available INTEGER NOT NULL DEFAULT 0 CHECK (inbound_line_available IN (0, 1)),
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(country_id, name)
);

CREATE TABLE IF NOT EXISTS route_naming_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    template TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    comment TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_route_naming_rules_single_active
    ON route_naming_rules(is_active) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS tariffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_id INTEGER NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    provider_prefix_id INTEGER REFERENCES provider_prefixes(id) ON DELETE RESTRICT,
    provider_currency_id INTEGER NOT NULL REFERENCES currencies(id) ON DELETE RESTRICT,
    price_in_provider_currency NUMERIC NOT NULL CHECK (price_in_provider_currency >= 0),
    conversion_rate_to_eur NUMERIC NOT NULL CHECK (conversion_rate_to_eur > 0),
    conversion_rate_date TEXT NOT NULL,
    currency_rate_id INTEGER REFERENCES currency_rates(id) ON DELETE RESTRICT,
    eur_price NUMERIC NOT NULL CHECK (eur_price >= 0),
    priority_status TEXT NOT NULL DEFAULT 'unknown' CHECK (priority_status IN ('priority', 'alternative', 'normal', 'unknown')),
    is_estimated INTEGER NOT NULL DEFAULT 0 CHECK (is_estimated IN (0, 1)),
    comment TEXT,
    valid_from TEXT,
    valid_to TEXT,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tariffs_current_business_key
    ON tariffs(country_id, provider_id, COALESCE(provider_prefix_id, 0)) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS tariff_change_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tariff_id INTEGER NOT NULL REFERENCES tariffs(id) ON DELETE RESTRICT,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    changed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    country_id INTEGER REFERENCES countries(id) ON DELETE RESTRICT,
    country_name_snapshot TEXT NOT NULL,
    provider_id INTEGER REFERENCES providers(id) ON DELETE RESTRICT,
    provider_name_snapshot TEXT NOT NULL,
    provider_prefix_id INTEGER REFERENCES provider_prefixes(id) ON DELETE RESTRICT,
    prefix_snapshot TEXT,
    old_provider_currency_id INTEGER REFERENCES currencies(id) ON DELETE RESTRICT,
    new_provider_currency_id INTEGER NOT NULL REFERENCES currencies(id) ON DELETE RESTRICT,
    old_price_in_provider_currency NUMERIC,
    new_price_in_provider_currency NUMERIC NOT NULL,
    old_conversion_rate_to_eur NUMERIC,
    new_conversion_rate_to_eur NUMERIC NOT NULL,
    old_conversion_rate_date TEXT,
    new_conversion_rate_date TEXT NOT NULL,
    old_eur_price NUMERIC,
    new_eur_price NUMERIC NOT NULL,
    eur_price_delta NUMERIC,
    reason TEXT,
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS phone_numbers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_id INTEGER NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
    provider_id INTEGER REFERENCES providers(id) ON DELETE RESTRICT,
    number TEXT NOT NULL,
    normalized_number TEXT NOT NULL UNIQUE,
    project_label TEXT,
    assignment_type TEXT NOT NULL,
    phone_type TEXT,
    tariff_label TEXT,
    status TEXT NOT NULL DEFAULT 'unknown' CHECK (status IN ('used', 'free', 'problem', 'unknown')),
    connection_cost NUMERIC CHECK (connection_cost IS NULL OR connection_cost >= 0),
    monthly_fee NUMERIC CHECK (monthly_fee IS NULL OR monthly_fee >= 0),
    outgoing_rate NUMERIC CHECK (outgoing_rate IS NULL OR outgoing_rate >= 0),
    incoming_rate NUMERIC CHECK (incoming_rate IS NULL OR incoming_rate >= 0),
    currency_id INTEGER REFERENCES currencies(id) ON DELETE RESTRICT,
    comment TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    review_required INTEGER NOT NULL DEFAULT 0 CHECK (review_required IN (0, 1)),
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deactivated_at TEXT,
    CHECK (number GLOB '[1-9]*' AND number NOT GLOB '*[^0-9]*' AND length(number) BETWEEN 7 AND 21),
    CHECK (normalized_number = number)
);

CREATE TABLE IF NOT EXISTS route_phone_numbers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE RESTRICT,
    phone_number_id INTEGER NOT NULL REFERENCES phone_numbers(id) ON DELETE RESTRICT,
    usage_type TEXT NOT NULL CHECK (usage_type IN ('cli', 'pool_member', 'main_number', 'backup_number', 'inbound_line', 'office_phone', 'sim_card', 'other')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    removed_at TEXT,
    added_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    removed_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    comment TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_route_phone_numbers_active_link
    ON route_phone_numbers(route_id, phone_number_id) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS route_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE RESTRICT,
    action TEXT NOT NULL,
    changed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    field_name TEXT,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    comment TEXT
);

CREATE TABLE IF NOT EXISTS phone_number_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_number_id INTEGER NOT NULL REFERENCES phone_numbers(id) ON DELETE RESTRICT,
    action TEXT NOT NULL,
    changed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    field_name TEXT,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    comment TEXT
);

CREATE TABLE IF NOT EXISTS route_phone_number_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE RESTRICT,
    phone_number_id INTEGER REFERENCES phone_numbers(id) ON DELETE RESTRICT,
    old_phone_number_id INTEGER REFERENCES phone_numbers(id) ON DELETE RESTRICT,
    new_phone_number_id INTEGER REFERENCES phone_numbers(id) ON DELETE RESTRICT,
    action TEXT NOT NULL,
    changed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    old_values TEXT,
    new_values TEXT,
    reason TEXT,
    comment TEXT
);

CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    comment TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE IF NOT EXISTS phone_number_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS phone_assignment_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS calling_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE RESTRICT,
    country_id INTEGER NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
    company_name TEXT NOT NULL,
    company_id_external TEXT NOT NULL CHECK (length(trim(company_id_external)) > 0),
    has_autorotation INTEGER NOT NULL DEFAULT 0 CHECK (has_autorotation IN (0, 1)),
    line_count INTEGER NOT NULL DEFAULT 0 CHECK (line_count >= 0),
    dial_set_count INTEGER NOT NULL DEFAULT 0 CHECK (dial_set_count >= 0),
    retry_interval_seconds INTEGER NOT NULL DEFAULT 0 CHECK (retry_interval_seconds >= 0),
    comment TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(server_id, country_id, company_id_external)
);


CREATE TABLE IF NOT EXISTS company_routing_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    calling_company_id INTEGER NOT NULL REFERENCES calling_companies(id) ON DELETE RESTRICT,
    country_id INTEGER NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE RESTRICT,
    route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
    routing_mode TEXT NOT NULL CHECK (routing_mode IN ('server_priority', 'campaign_route', 'autorotation', 'mixed')),
    has_autorotation INTEGER NOT NULL DEFAULT 0 CHECK (has_autorotation IN (0, 1)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    comment TEXT,
    valid_from TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_to TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    CHECK ((is_active = 1 AND valid_to IS NULL) OR is_active = 0)
);

CREATE TABLE IF NOT EXISTS change_reasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS provider_change_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at TEXT NOT NULL,
    country_id INTEGER NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
    company_id INTEGER REFERENCES calling_companies(id) ON DELETE RESTRICT,
    company_name_snapshot TEXT,
    has_autorotation_snapshot INTEGER CHECK (has_autorotation_snapshot IN (0, 1) OR has_autorotation_snapshot IS NULL),
    route_before_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
    provider_before_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    provider_prefix_before_id INTEGER REFERENCES provider_prefixes(id) ON DELETE RESTRICT,
    tariff_before_id INTEGER REFERENCES tariffs(id) ON DELETE RESTRICT,
    price_before_provider_currency_id INTEGER REFERENCES currencies(id) ON DELETE RESTRICT,
    price_before_in_provider_currency NUMERIC,
    price_before_conversion_rate_to_eur NUMERIC,
    price_before_conversion_rate_date TEXT,
    price_before_eur NUMERIC,
    route_after_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
    provider_after_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    provider_prefix_after_id INTEGER REFERENCES provider_prefixes(id) ON DELETE RESTRICT,
    tariff_after_id INTEGER REFERENCES tariffs(id) ON DELETE RESTRICT,
    price_after_provider_currency_id INTEGER REFERENCES currencies(id) ON DELETE RESTRICT,
    price_after_in_provider_currency NUMERIC,
    price_after_conversion_rate_to_eur NUMERIC,
    price_after_conversion_rate_date TEXT,
    price_after_eur NUMERIC,
    price_delta_eur NUMERIC,
    provider_changed INTEGER NOT NULL CHECK (provider_changed IN (0, 1)),
    reason_id INTEGER REFERENCES change_reasons(id) ON DELETE RESTRICT,
    reason_text TEXT,
    comment TEXT,
    telegram_status TEXT NOT NULL DEFAULT 'not_sent' CHECK (telegram_status IN ('not_sent', 'sent', 'failed', 'disabled')),
    telegram_sent_at TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS provider_change_log_servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_change_log_id INTEGER NOT NULL REFERENCES provider_change_logs(id) ON DELETE CASCADE,
    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider_change_log_id, server_id)
);

CREATE TABLE IF NOT EXISTS server_route_priorities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_id INTEGER NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE RESTRICT,
    current_route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE RESTRICT,
    previous_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
    provider_change_log_id INTEGER REFERENCES provider_change_logs(id) ON DELETE RESTRICT,
    changed_at TEXT,
    changed_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    reason TEXT,
    comment TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(country_id, server_id)
);


CREATE TABLE IF NOT EXISTS routing_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_at TEXT NOT NULL,
    apply_scope TEXT NOT NULL CHECK (apply_scope IN ('none', 'server_priority', 'campaign_setting')),
    reason TEXT NOT NULL,
    country_id INTEGER REFERENCES countries(id) ON DELETE RESTRICT,
    server_id INTEGER REFERENCES servers(id) ON DELETE RESTRICT,
    provider_id INTEGER REFERENCES providers(id) ON DELETE RESTRICT,
    affected_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
    old_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
    new_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
    calling_company_id INTEGER REFERENCES calling_companies(id) ON DELETE RESTRICT,
    company_change_type TEXT CHECK (company_change_type IN ('enable_autorotation', 'disable_autorotation', 'set_campaign_route', 'remove_campaign_route') OR company_change_type IS NULL),
    old_company_routing_mode TEXT,
    new_company_routing_mode TEXT,
    old_company_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
    new_company_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
    old_company_has_autorotation INTEGER CHECK (old_company_has_autorotation IN (0, 1) OR old_company_has_autorotation IS NULL),
    new_company_has_autorotation INTEGER CHECK (new_company_has_autorotation IN (0, 1) OR new_company_has_autorotation IS NULL),
    comment TEXT NOT NULL,
    snapshot_json TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    deactivation_reason TEXT,
    deactivated_at TEXT,
    deactivated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_routing_events_event_at ON routing_events(event_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_routing_events_scope ON routing_events(apply_scope);
CREATE INDEX IF NOT EXISTS idx_routing_events_active ON routing_events(is_active);

CREATE TABLE IF NOT EXISTS routing_event_servers (
    id INTEGER PRIMARY KEY,
    routing_event_id INTEGER NOT NULL REFERENCES routing_events(id) ON DELETE RESTRICT,
    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE RESTRICT,
    old_route_id INTEGER REFERENCES routes(id) ON DELETE RESTRICT,
    new_route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE RESTRICT,
    server_route_priority_id INTEGER REFERENCES server_route_priorities(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'applied' CHECK (status IN ('applied', 'skipped_noop')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_routing_event_servers_event ON routing_event_servers(routing_event_id);
CREATE INDEX IF NOT EXISTS idx_routing_event_servers_server ON routing_event_servers(server_id);
CREATE INDEX IF NOT EXISTS idx_routing_event_servers_new_route ON routing_event_servers(new_route_id);

CREATE TABLE IF NOT EXISTS change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    change_type TEXT NOT NULL,
    changed_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    old_values TEXT,
    new_values TEXT,
    summary TEXT,
    comment TEXT,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS import_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('routes', 'tariffs', 'phone_numbers', 'calling_companies', 'dictionaries')),
    mode TEXT NOT NULL CHECK (mode IN ('append_update', 'replace_section')),
    file_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'previewed', 'running', 'completed', 'failed', 'cancelled')),
    total_rows INTEGER,
    new_rows INTEGER,
    duplicate_rows INTEGER,
    skipped_rows INTEGER,
    updated_rows INTEGER,
    replaced_rows INTEGER,
    error_rows INTEGER,
    preview_data TEXT,
    summary TEXT,
    error_report TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TEXT,
    comment TEXT
);

CREATE TABLE IF NOT EXISTS telegram_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    is_enabled INTEGER NOT NULL DEFAULT 0 CHECK (is_enabled IN (0, 1)),
    chat_id TEXT,
    bot_token_secret_ref TEXT,
    message_template TEXT,
    last_test_status TEXT CHECK (last_test_status IN ('success', 'failed') OR last_test_status IS NULL),
    last_test_at TEXT,
    last_test_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    last_test_error TEXT,
    updated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_routes_country_id ON routes(country_id);
CREATE INDEX IF NOT EXISTS idx_routes_provider_id ON routes(provider_id);
CREATE INDEX IF NOT EXISTS idx_routes_name ON routes(name);
CREATE INDEX IF NOT EXISTS idx_routes_is_actual ON routes(is_actual);
CREATE INDEX IF NOT EXISTS idx_tariffs_country_id ON tariffs(country_id);
CREATE INDEX IF NOT EXISTS idx_tariffs_provider_id ON tariffs(provider_id);
CREATE INDEX IF NOT EXISTS idx_tariffs_is_current ON tariffs(is_current);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_country_id ON phone_numbers(country_id);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_provider_id ON phone_numbers(provider_id);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_status ON phone_numbers(status);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_assignment_type ON phone_numbers(assignment_type);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_number ON phone_numbers(number);
CREATE INDEX IF NOT EXISTS idx_route_phone_numbers_route_id ON route_phone_numbers(route_id);
CREATE INDEX IF NOT EXISTS idx_route_phone_numbers_phone_number_id ON route_phone_numbers(phone_number_id);
CREATE INDEX IF NOT EXISTS idx_provider_change_logs_changed_at ON provider_change_logs(changed_at);
CREATE INDEX IF NOT EXISTS idx_provider_change_logs_country_id ON provider_change_logs(country_id);
CREATE INDEX IF NOT EXISTS idx_calling_companies_server_id ON calling_companies(server_id);
CREATE INDEX IF NOT EXISTS idx_calling_companies_country_id ON calling_companies(country_id);
CREATE INDEX IF NOT EXISTS idx_calling_companies_external_id ON calling_companies(company_id_external);
CREATE INDEX IF NOT EXISTS idx_company_routing_settings_company_id ON company_routing_settings(calling_company_id);
CREATE INDEX IF NOT EXISTS idx_company_routing_settings_country_id ON company_routing_settings(country_id);
CREATE INDEX IF NOT EXISTS idx_company_routing_settings_server_id ON company_routing_settings(server_id);
CREATE INDEX IF NOT EXISTS idx_company_routing_settings_route_id ON company_routing_settings(route_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_company_routing_settings_one_active ON company_routing_settings(calling_company_id) WHERE is_active = 1 AND valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_server_route_priorities_country_id ON server_route_priorities(country_id);
CREATE INDEX IF NOT EXISTS idx_server_route_priorities_server_id ON server_route_priorities(server_id);
CREATE INDEX IF NOT EXISTS idx_change_log_changed_at ON change_log(changed_at);
CREATE INDEX IF NOT EXISTS idx_change_log_entity ON change_log(entity_type, entity_id);
