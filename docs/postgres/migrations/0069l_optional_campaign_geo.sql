-- Stage 69L: a campaign without one classified GEO is a multi-GEO campaign.
-- PostgreSQL accepts DROP NOT NULL repeatedly; existing values and the FK remain unchanged.
ALTER TABLE calling_companies ALTER COLUMN country_id DROP NOT NULL;

-- The existing application rule is globally unique company_id_external. This narrower
-- database guard additionally closes PostgreSQL's NULL hole in the legacy identity key.
CREATE UNIQUE INDEX IF NOT EXISTS ux_calling_companies_multi_geo_identity
    ON calling_companies(server_id, company_id_external) WHERE country_id IS NULL;
