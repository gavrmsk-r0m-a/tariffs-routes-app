-- Repair English GEO rows accidentally created by calling-company import.
-- Safe to run repeatedly: references are repointed to the existing canonical
-- Russian country row, then now-unreferenced alias rows are removed.

CREATE TEMP TABLE campaign_country_alias_repair (
    alias_name TEXT PRIMARY KEY,
    iso_code TEXT NOT NULL,
    canonical_name TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO campaign_country_alias_repair(alias_name, iso_code, canonical_name) VALUES
    ('Albania', 'AL', 'Албания'),
    ('Argentina', 'AR', 'Аргентина'),
    ('Austria', 'AT', 'Австрия'),
    ('Belarus', 'BY', 'Беларусь'),
    ('Belgium', 'BE', 'Бельгия'),
    ('Bolivia', 'BO', 'Боливия'),
    ('Bosnia', 'BA', 'Босния'),
    ('Bosnia and Herzegovina', 'BA', 'Босния'),
    ('Brazil', 'BR', 'Бразилия'),
    ('Bulgaria', 'BG', 'Болгария'),
    ('Chile', 'CL', 'Чили'),
    ('Colombia', 'CO', 'Колумбия'),
    ('Costa Rica', 'CR', 'Коста-Рика'),
    ('Croatia', 'HR', 'Хорватия'),
    ('Cyprus', 'CY', 'Кипр'),
    ('Czech', 'CZ', 'Чехия'),
    ('Czechia', 'CZ', 'Чехия'),
    ('Czech Republic', 'CZ', 'Чехия'),
    ('Denmark', 'DK', 'Дания'),
    ('Dominican Republic', 'DO', 'Доминиканская Республика'),
    ('Ecuador', 'EC', 'Эквадор'),
    ('El Salvador', 'SV', 'Сальвадор'),
    ('Estonia', 'EE', 'Эстония'),
    ('Finland', 'FI', 'Финляндия'),
    ('France', 'FR', 'Франция'),
    ('Germany', 'DE', 'Германия'),
    ('Greece', 'GR', 'Греция'),
    ('Guatemala', 'GT', 'Гватемала'),
    ('Honduras', 'HN', 'Гондурас'),
    ('Hungary', 'HU', 'Венгрия'),
    ('Italy', 'IT', 'Италия'),
    ('Latvia', 'LV', 'Латвия'),
    ('Lithuania', 'LT', 'Литва'),
    ('Mexico', 'MX', 'Мексика'),
    ('Moldova', 'MD', 'Молдова'),
    ('Netherlands', 'NL', 'Нидерланды'),
    ('Nicaragua', 'NI', 'Никарагуа'),
    ('Norway', 'NO', 'Норвегия'),
    ('Panama', 'PA', 'Панама'),
    ('Paraguay', 'PY', 'Парагвай'),
    ('Peru', 'PE', 'Перу'),
    ('Poland', 'PL', 'Польша'),
    ('Portugal', 'PT', 'Португалия'),
    ('Romania', 'RO', 'Румыния'),
    ('Serbia', 'RS', 'Сербия'),
    ('Singapore', 'SG', 'Сингапур'),
    ('Slovakia', 'SK', 'Словакия'),
    ('Slovenia', 'SI', 'Словения'),
    ('Spain', 'ES', 'Испания'),
    ('Sweden', 'SE', 'Швеция'),
    ('Switzerland', 'CH', 'Швейцария'),
    ('Turkey', 'TR', 'Турция'),
    ('UAE', 'AE', 'ОАЭ'),
    ('United Arab Emirates', 'AE', 'ОАЭ'),
    ('UK', 'GB', 'Великобритания'),
    ('United Kingdom', 'GB', 'Великобритания'),
    ('Uruguay', 'UY', 'Уругвай'),
    ('Venezuela', 'VE', 'Венесуэла');

-- Only pairs for which the canonical Russian dictionary row already exists
-- are eligible. This migration never invents a new country.
CREATE TEMP TABLE campaign_country_alias_pairs ON COMMIT DROP AS
SELECT alias.id AS alias_id, canonical.id AS canonical_id
FROM campaign_country_alias_repair mapping
JOIN countries alias
  ON LOWER(TRIM(alias.name)) = LOWER(TRIM(mapping.alias_name))
JOIN countries canonical
  ON UPPER(COALESCE(canonical.code, '')) = UPPER(mapping.iso_code)
  OR canonical.name = mapping.canonical_name
WHERE alias.id <> canonical.id;

UPDATE calling_companies target
SET country_id = pairs.canonical_id
FROM campaign_country_alias_pairs pairs
WHERE target.country_id = pairs.alias_id;

UPDATE company_routing_settings target
SET country_id = pairs.canonical_id
FROM campaign_country_alias_pairs pairs
WHERE target.country_id = pairs.alias_id;

UPDATE routing_events target
SET country_id = pairs.canonical_id
FROM campaign_country_alias_pairs pairs
WHERE target.country_id = pairs.alias_id;

UPDATE provider_change_logs target
SET country_id = pairs.canonical_id
FROM campaign_country_alias_pairs pairs
WHERE target.country_id = pairs.alias_id;

UPDATE tariff_change_history target
SET country_id = pairs.canonical_id
FROM campaign_country_alias_pairs pairs
WHERE target.country_id = pairs.alias_id;

-- Do not blindly rewrite business objects such as routes/tariffs/server
-- priorities because those tables have business-key uniqueness constraints.
-- The alias is deleted only when no country FK references remain anywhere.
DELETE FROM countries country
USING campaign_country_alias_pairs pairs
WHERE country.id = pairs.alias_id
  AND NOT EXISTS (SELECT 1 FROM routes r WHERE r.country_id = country.id)
  AND NOT EXISTS (SELECT 1 FROM tariffs t WHERE t.country_id = country.id)
  AND NOT EXISTS (SELECT 1 FROM tariff_change_history h WHERE h.country_id = country.id)
  AND NOT EXISTS (SELECT 1 FROM phone_numbers p WHERE p.country_id = country.id)
  AND NOT EXISTS (SELECT 1 FROM calling_companies c WHERE c.country_id = country.id)
  AND NOT EXISTS (SELECT 1 FROM company_routing_settings s WHERE s.country_id = country.id)
  AND NOT EXISTS (SELECT 1 FROM provider_change_logs l WHERE l.country_id = country.id)
  AND NOT EXISTS (SELECT 1 FROM server_route_priorities p WHERE p.country_id = country.id)
  AND NOT EXISTS (SELECT 1 FROM routing_events e WHERE e.country_id = country.id);
