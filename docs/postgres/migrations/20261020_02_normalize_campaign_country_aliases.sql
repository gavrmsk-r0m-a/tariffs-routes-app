-- Repair English GEO rows accidentally created by calling-company import.
-- Safe to run repeatedly: references are repointed to the existing canonical
-- Russian country row, then now-unreferenced alias rows are removed.

CREATE TEMP TABLE campaign_country_alias_repair (
    alias_name TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO campaign_country_alias_repair(alias_name, canonical_name) VALUES
    ('Albania', 'Албания'),
    ('Argentina', 'Аргентина'),
    ('Austria', 'Австрия'),
    ('Belarus', 'Беларусь'),
    ('Belgium', 'Бельгия'),
    ('Bolivia', 'Боливия'),
    ('Bosnia', 'Босния'),
    ('Bosnia and Herzegovina', 'Босния'),
    ('Brazil', 'Бразилия'),
    ('Bulgaria', 'Болгария'),
    ('Chile', 'Чили'),
    ('Colombia', 'Колумбия'),
    ('Costa Rica', 'Коста-Рика'),
    ('Croatia', 'Хорватия'),
    ('Cyprus', 'Кипр'),
    ('Czech', 'Чехия'),
    ('Czechia', 'Чехия'),
    ('Czech Republic', 'Чехия'),
    ('Denmark', 'Дания'),
    ('Dominican Republic', 'Доминиканская Республика'),
    ('Ecuador', 'Эквадор'),
    ('El Salvador', 'Сальвадор'),
    ('Estonia', 'Эстония'),
    ('Finland', 'Финляндия'),
    ('France', 'Франция'),
    ('Germany', 'Германия'),
    ('Greece', 'Греция'),
    ('Guatemala', 'Гватемала'),
    ('Honduras', 'Гондурас'),
    ('Hungary', 'Венгрия'),
    ('Italy', 'Италия'),
    ('Latvia', 'Латвия'),
    ('Lithuania', 'Литва'),
    ('Mexico', 'Мексика'),
    ('Moldova', 'Молдова'),
    ('Netherlands', 'Нидерланды'),
    ('Nicaragua', 'Никарагуа'),
    ('Norway', 'Норвегия'),
    ('Panama', 'Панама'),
    ('Paraguay', 'Парагвай'),
    ('Peru', 'Перу'),
    ('Poland', 'Польша'),
    ('Portugal', 'Португалия'),
    ('Romania', 'Румыния'),
    ('Serbia', 'Сербия'),
    ('Singapore', 'Сингапур'),
    ('Slovakia', 'Словакия'),
    ('Slovenia', 'Словения'),
    ('Spain', 'Испания'),
    ('Sweden', 'Швеция'),
    ('Switzerland', 'Швейцария'),
    ('Turkey', 'Турция'),
    ('UAE', 'ОАЭ'),
    ('United Arab Emirates', 'ОАЭ'),
    ('UK', 'Великобритания'),
    ('United Kingdom', 'Великобритания'),
    ('Uruguay', 'Уругвай'),
    ('Venezuela', 'Венесуэла');

-- Only pairs for which the canonical Russian dictionary row already exists
-- are eligible. This migration never invents a new country.
CREATE TEMP TABLE campaign_country_alias_pairs ON COMMIT DROP AS
SELECT alias.id AS alias_id, canonical.id AS canonical_id
FROM campaign_country_alias_repair mapping
JOIN countries alias
  ON LOWER(TRIM(alias.name)) = LOWER(TRIM(mapping.alias_name))
JOIN countries canonical
  ON canonical.name = mapping.canonical_name
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
