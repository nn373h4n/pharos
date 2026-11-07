-- Synthetic data for the load run (M5-08, D5). Run by scripts/loadtest.sh as the database superuser
-- inside a throwaway stack; never against a real installation.
--
--   psql -v org_slug=load -v articles=30000 -v people=300 -f seed.sql
--
-- Everything is invented: articles are "Load N" with a made-up address, people are "load-N".
-- Six in ten articles belong to a cluster of about three (ADR-0021), so the collapse of copies is
-- part of what the timing measures, as in the integration test of the feed.
\set ON_ERROR_STOP on

SELECT id AS org_id FROM organizations WHERE slug = :'org_slug' \gset

INSERT INTO users (org_id, issuer, subject, display_name, role)
SELECT CAST(:'org_id' AS uuid), 'local', 'load-' || g, 'Load ' || g, 'member'
FROM generate_series(1, :people) g;

-- Each person has finished onboarding, with the professions of the public catalogue shared out
-- evenly (the Home feed answers 409 before onboarding).
INSERT INTO user_profiles (user_id, org_id, profession_id, ai_level, onboarding_started_at,
                           onboarding_completed_at)
SELECT u.id, u.org_id, p.id, 'beginner', now(), now()
FROM (SELECT id, org_id, row_number() OVER (ORDER BY subject) AS n
      FROM users WHERE org_id = CAST(:'org_id' AS uuid) AND subject LIKE 'load-%') u
JOIN (SELECT id, row_number() OVER (ORDER BY code) - 1 AS n
      FROM professions WHERE scope = 'public') p
  ON p.n = (u.n - 1) % (SELECT count(*) FROM professions WHERE scope = 'public');

INSERT INTO items (org_id, canonical_url, url_hash, title, content_sha256, published_at, summary,
                   category, quality, enriched_at, enrichment_version, cluster_id)
SELECT CAST(:'org_id' AS uuid), 'https://news.example.org/load' || g,
       md5(g::text) || md5('x' || g), 'Load ' || g, md5(g::text) || md5('y' || g),
       now() - (g || ' minutes')::interval, 'Summary ' || g,
       (ARRAY['tool','technique','tutorial','news'])[1 + g % 4], 1 + g % 5, now(), 'v1',
       CASE WHEN g % 10 < 6 THEN CAST(md5('c' || (g / 3)) AS uuid) END
FROM generate_series(1, :articles) g;

-- Each article fits about three in ten of the professions, as a model would have judged it.
INSERT INTO item_professions (org_id, item_id, profession_code, relevance)
SELECT i.org_id, i.id, p.code, (random() * 100)::int
FROM items i CROSS JOIN professions p
WHERE p.scope = 'public' AND i.org_id = CAST(:'org_id' AS uuid) AND i.title LIKE 'Load %'
  AND random() < 0.3;

-- What autovacuum does in a real installation, so the planner knows the sizes.
ANALYZE items;
ANALYZE item_professions;
ANALYZE users;
ANALYZE user_profiles;
