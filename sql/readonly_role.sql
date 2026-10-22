-- =====================================================================
-- Read-only database user for the public web app.
-- The app only ever reads, so give it a user that CAN'T change anything: if its password
-- leaks, nobody can alter or delete your data.
--
-- 1. Create the role. In Neon: Roles & Databases -> add a role named app_reader (copy its password),
--    OR run the next line yourself (replace the password with a long random one):
--      CREATE ROLE app_reader WITH LOGIN PASSWORD 'replace-with-a-long-random-password';
-- 2. Run the lines below once in the Neon SQL Editor, signed in as the project owner.
-- =====================================================================
GRANT USAGE ON SCHEMA public TO app_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_reader;
-- views and tables created later by the owner (for example when upcoming.py setup recreates the views)
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO app_reader;
