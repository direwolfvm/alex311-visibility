-- Empirical per-category question schema, mined from the Open311-style
-- `attributes` array every enriched record carries (raw_detail->'attributes').
-- The portal's service catalog (getServiceTypes) exposes NO question schema,
-- so this is the source of truth for a schema-driven submission form.
--
--   code / question / datatype / order  -> straight from the portal
--   pct  (share of the category's records carrying the question)
--        -> proxy for "required": >=95% always asked; <50% conditional
--   answer_vocab -> every option ever chosen on list-type questions
--
-- Run:  psql "$DATABASE_URL" -At -f scripts/mine_question_schema.sql > docs/data/question-schema.mined.json
WITH base AS (
  SELECT service_name, service_request_id, raw_detail->'attributes' attrs
  FROM service_requests
  WHERE raw_detail IS NOT NULL AND jsonb_typeof(raw_detail->'attributes') = 'array'),
tot AS (SELECT service_name, count(*) n FROM base GROUP BY service_name),
q AS (
  SELECT b.service_name, b.service_request_id, a->>'code' code, (a->>'order')::int ord,
         a->>'datatype' datatype, a->>'description' question, v->>'answer' answer
  FROM base b, jsonb_array_elements(b.attrs) a
  LEFT JOIN LATERAL jsonb_array_elements(a->'values') v ON true),
agg AS (
  SELECT q.service_name, code, min(ord) ord, datatype, question,
         count(DISTINCT service_request_id) present, tot.n total,
         round(100.0 * count(DISTINCT service_request_id) / tot.n, 1) pct,
         jsonb_agg(DISTINCT answer) FILTER (WHERE answer IS NOT NULL AND datatype LIKE '%list%') answer_vocab
  FROM q JOIN tot USING (service_name)
  GROUP BY q.service_name, code, datatype, question, tot.n)
SELECT json_agg(agg ORDER BY service_name, ord) FROM agg;
