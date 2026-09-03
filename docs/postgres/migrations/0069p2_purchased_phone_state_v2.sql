-- Purchased phone state model v2. Safe to run repeatedly.
BEGIN;

ALTER TABLE phone_numbers
    ADD COLUMN IF NOT EXISTS is_problematic BOOLEAN NOT NULL DEFAULT false;

-- An active route link is stronger evidence of use for an active provider number.
UPDATE phone_numbers pn
SET status = 'used',
    review_required = true,
    is_problematic = pn.is_problematic OR pn.status = 'problem'
WHERE pn.is_active IS TRUE
  AND pn.status IN ('free', 'unused', 'unknown', 'problem')
  AND EXISTS (
      SELECT 1 FROM route_phone_numbers rpn
      WHERE rpn.phone_number_id = pn.id AND rpn.is_active IS TRUE
  );

UPDATE phone_numbers
SET status = CASE
        WHEN is_active IS FALSE THEN 'unused'
        WHEN status = 'problem' THEN 'unknown'
        WHEN status = 'free' THEN 'unused'
        ELSE status
    END,
    is_problematic = is_problematic OR status = 'problem',
    review_required = review_required OR status = 'problem' OR is_problematic;

-- A migration has no user actor. Close impossible links without fabricating audit users;
-- immutable user-authored history remains untouched.
UPDATE route_phone_numbers rpn
SET is_active = false, removed_at = COALESCE(removed_at, CURRENT_TIMESTAMP)
FROM phone_numbers pn
WHERE rpn.phone_number_id = pn.id
  AND rpn.is_active IS TRUE
  AND (pn.is_active IS FALSE OR pn.status <> 'used');

ALTER TABLE phone_numbers DROP CONSTRAINT IF EXISTS ck_phone_numbers_status;
ALTER TABLE phone_numbers ADD CONSTRAINT ck_phone_numbers_status
    CHECK (status IN ('used', 'unused', 'unknown'));
ALTER TABLE phone_numbers DROP CONSTRAINT IF EXISTS ck_phone_numbers_problematic_review;
ALTER TABLE phone_numbers ADD CONSTRAINT ck_phone_numbers_problematic_review
    CHECK (is_problematic IS FALSE OR review_required IS TRUE);

COMMIT;
