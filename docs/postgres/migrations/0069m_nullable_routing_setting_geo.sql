-- Stage 69M: multi-GEO campaigns may own a campaign-level routing state.
-- PostgreSQL DROP NOT NULL is idempotent and preserves the existing FK and rows.
ALTER TABLE company_routing_settings
    ALTER COLUMN country_id DROP NOT NULL;
