\set ON_ERROR_STOP off
\set VERBOSITY verbose
\pset pager off
\echo ### 0 setup (superuser): scratch admin role and scratch template database
CREATE ROLE cairn_fv4_admin WITH LOGIN CREATEROLE CREATEDB BYPASSRLS NOSUPERUSER NOREPLICATION PASSWORD 'fv4-admin-pw';
CREATE DATABASE cairn_fv4_tmpl;
\c cairn_fv4_tmpl postgres
CREATE EXTENSION vector;
CREATE EXTENSION btree_gist;
\c postgres postgres
ALTER DATABASE cairn_fv4_tmpl WITH IS_TEMPLATE true;
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication FROM pg_roles WHERE rolname = 'cairn_fv4_admin';

\echo ### 1 as the admin: create the three roles (CREATE with every attribute)
\c postgres cairn_fv4_admin
SELECT current_user, current_database();
CREATE ROLE cairn_fv4_owner WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT PASSWORD 'fv4-owner-pw';
CREATE ROLE cairn_fv4_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD 'fv4-app-pw';
CREATE ROLE cairn_fv4_dbos WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT PASSWORD 'fv4-dbos-pw';
\echo -- memberships after create (creator gets ADMIN only, createrole_self_grant is empty)
SELECT m.rolname AS member, g.rolname AS granted, am.admin_option, am.inherit_option, am.set_option FROM pg_auth_members am JOIN pg_roles m ON m.oid = am.member JOIN pg_roles g ON g.oid = am.roleid WHERE g.rolname LIKE 'cairn_fv4_%' ORDER BY 1, 2;

\echo ### 2 as the admin: each ALTER ROLE attribute clause on a role it created
ALTER ROLE cairn_fv4_owner WITH LOGIN;
ALTER ROLE cairn_fv4_owner WITH NOCREATEDB;
ALTER ROLE cairn_fv4_owner WITH NOCREATEROLE;
ALTER ROLE cairn_fv4_owner WITH INHERIT;
ALTER ROLE cairn_fv4_app WITH NOINHERIT;
ALTER ROLE cairn_fv4_owner WITH PASSWORD 'fv4-owner-pw-2';
ALTER ROLE cairn_fv4_owner WITH NOSUPERUSER;
ALTER ROLE cairn_fv4_owner WITH NOREPLICATION;
ALTER ROLE cairn_fv4_owner WITH NOBYPASSRLS;
ALTER ROLE cairn_fv4_owner WITH LOGIN NOCREATEDB NOCREATEROLE INHERIT PASSWORD 'fv4-owner-pw-3';
\echo -- the managed ALTER form (LOGIN NOCREATEDB NOCREATEROLE [NO]INHERIT PASSWORD) on all three
ALTER ROLE cairn_fv4_owner WITH LOGIN NOCREATEDB NOCREATEROLE INHERIT PASSWORD 'fv4-owner-pw-4';
ALTER ROLE cairn_fv4_app WITH LOGIN NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD 'fv4-app-pw-4';
ALTER ROLE cairn_fv4_dbos WITH LOGIN NOCREATEDB NOCREATEROLE INHERIT PASSWORD 'fv4-dbos-pw-4';
\echo -- full superuser-mode ALTER form by the non-superuser (expected to fail)
ALTER ROLE cairn_fv4_owner WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT PASSWORD 'fv4-owner-pw-5';
\echo -- plaintext password statement (what OQ-1 sends), and a SCRAM verifier literal
ALTER ROLE cairn_fv4_owner WITH LOGIN NOCREATEDB NOCREATEROLE INHERIT PASSWORD 'SCRAM-SHA-256$4096:c2FsdHNhbHRzYWx0c2E=$c3RvcmVka2V5Cg==:c2VydmVya2V5Cg==';

\echo ### 3 CREATE DATABASE ... OWNER before the SET grant (expected to fail)
CREATE DATABASE cairn_fv4_noset OWNER cairn_fv4_owner;

\echo ### 4 the SET grants (WITH INHERIT FALSE, SET TRUE), twice (idempotent)
GRANT cairn_fv4_owner TO cairn_fv4_admin WITH INHERIT FALSE, SET TRUE;
GRANT cairn_fv4_dbos TO cairn_fv4_admin WITH INHERIT FALSE, SET TRUE;
GRANT cairn_fv4_owner TO cairn_fv4_admin WITH INHERIT FALSE, SET TRUE;
GRANT cairn_fv4_dbos TO cairn_fv4_admin WITH INHERIT FALSE, SET TRUE;
SELECT m.rolname AS member, g.rolname AS granted, am.admin_option, am.inherit_option, am.set_option FROM pg_auth_members am JOIN pg_roles m ON m.oid = am.member JOIN pg_roles g ON g.oid = am.roleid WHERE g.rolname LIKE 'cairn_fv4_%' ORDER BY 1, 2;

\echo ### 5 CREATE DATABASE ... OWNER after the SET grant (from template1 and from the scratch template)
CREATE DATABASE cairn_fv4_core OWNER cairn_fv4_owner;
CREATE DATABASE cairn_fv4_dbosdb OWNER cairn_fv4_dbos;
CREATE DATABASE cairn_fv4_fromtmpl TEMPLATE cairn_fv4_tmpl OWNER cairn_fv4_owner;
SELECT datname, pg_get_userbyid(datdba) AS owner, datacl FROM pg_database WHERE datname LIKE 'cairn_fv4_%' ORDER BY 1;

\echo ### 6 database privilege statements run directly by the admin (not owner, INHERIT FALSE)
REVOKE CONNECT, TEMPORARY ON DATABASE cairn_fv4_core FROM PUBLIC;
GRANT CONNECT ON DATABASE cairn_fv4_core TO cairn_fv4_owner;
GRANT CONNECT ON DATABASE cairn_fv4_core TO cairn_fv4_app;
REVOKE CONNECT ON DATABASE cairn_fv4_core FROM cairn_fv4_dbos;

\echo ### 7 the same statements under SET ROLE <owner>
SET ROLE cairn_fv4_owner;
SELECT current_user, session_user;
REVOKE CONNECT, TEMPORARY ON DATABASE cairn_fv4_core FROM PUBLIC;
GRANT CONNECT ON DATABASE cairn_fv4_core TO cairn_fv4_owner;
GRANT CONNECT ON DATABASE cairn_fv4_core TO cairn_fv4_app;
REVOKE CONNECT ON DATABASE cairn_fv4_core FROM cairn_fv4_dbos;
RESET ROLE;
SET ROLE cairn_fv4_dbos;
REVOKE CONNECT, TEMPORARY ON DATABASE cairn_fv4_dbosdb FROM PUBLIC;
GRANT CONNECT ON DATABASE cairn_fv4_dbosdb TO cairn_fv4_dbos;
REVOKE CONNECT ON DATABASE cairn_fv4_dbosdb FROM cairn_fv4_app;
REVOKE CONNECT ON DATABASE cairn_fv4_dbosdb FROM cairn_fv4_owner;
RESET ROLE;
SELECT datname, datacl FROM pg_database WHERE datname LIKE 'cairn_fv4_%' ORDER BY 1;

\echo ### 8 can the admin connect to cairn_fv4_core after CONNECT was revoked from PUBLIC? (no CONNECT grant for the admin)
SELECT has_database_privilege('cairn_fv4_admin', 'cairn_fv4_core', 'CONNECT') AS admin_can_connect;
\echo -- grant the admin CONNECT under SET ROLE owner, then check
SET ROLE cairn_fv4_owner;
GRANT CONNECT ON DATABASE cairn_fv4_core TO cairn_fv4_admin;
RESET ROLE;
SELECT has_database_privilege('cairn_fv4_admin', 'cairn_fv4_core', 'CONNECT') AS admin_can_connect;
\c cairn_fv4_core cairn_fv4_admin
SELECT current_user, current_database();
\echo -- revoke it again (under SET ROLE owner) from the maintenance database
\c postgres cairn_fv4_admin
SET ROLE cairn_fv4_owner;
REVOKE CONNECT ON DATABASE cairn_fv4_core FROM cairn_fv4_admin;
RESET ROLE;
SELECT datacl FROM pg_database WHERE datname = 'cairn_fv4_core';

\echo ### 9 database created from the scratch template: revoke CONNECT from PUBLIC, grant the admin CONNECT (under SET ROLE owner), connect
SET ROLE cairn_fv4_owner;
REVOKE CONNECT, TEMPORARY ON DATABASE cairn_fv4_fromtmpl FROM PUBLIC;
GRANT CONNECT ON DATABASE cairn_fv4_fromtmpl TO cairn_fv4_owner;
GRANT CONNECT ON DATABASE cairn_fv4_fromtmpl TO cairn_fv4_admin;
RESET ROLE;
\c cairn_fv4_fromtmpl cairn_fv4_admin
SELECT current_user, current_database();
SELECT extname, extversion FROM pg_extension ORDER BY 1;
\echo -- IF NOT EXISTS on extensions that the template already carries
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;
\echo -- without IF NOT EXISTS (expected: already exists)
CREATE EXTENSION vector;
\echo -- public schema revoke under SET ROLE owner
SET ROLE cairn_fv4_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
RESET ROLE;
SELECT nspname, pg_get_userbyid(nspowner) AS owner, nspacl FROM pg_namespace WHERE nspname = 'public';
\echo -- the same revoke directly as the admin
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

\echo ### 10 extensions in a database created from template1 (cairn_fv4_noext): connect needs CONNECT
\c postgres cairn_fv4_admin
CREATE DATABASE cairn_fv4_noext OWNER cairn_fv4_owner;
SET ROLE cairn_fv4_owner;
REVOKE CONNECT, TEMPORARY ON DATABASE cairn_fv4_noext FROM PUBLIC;
GRANT CONNECT ON DATABASE cairn_fv4_noext TO cairn_fv4_owner;
GRANT CONNECT ON DATABASE cairn_fv4_noext TO cairn_fv4_admin;
RESET ROLE;
\c cairn_fv4_noext cairn_fv4_admin
SELECT current_user, current_database();
\echo -- 10a vector as the admin (not trusted: expected to fail)
CREATE EXTENSION IF NOT EXISTS vector;
\echo -- 10b vector under SET ROLE owner (expected to fail)
SET ROLE cairn_fv4_owner;
CREATE EXTENSION IF NOT EXISTS vector;
RESET ROLE;
\echo -- 10c btree_gist as the admin with no extra grant
CREATE EXTENSION IF NOT EXISTS btree_gist;
\echo -- 10d btree_gist under SET ROLE owner (database owner holds CREATE on the database; public schema CREATE after PG15 only for the owner)
SET ROLE cairn_fv4_owner;
CREATE EXTENSION IF NOT EXISTS btree_gist;
SELECT extname, pg_get_userbyid(extowner) AS ext_owner FROM pg_extension WHERE extname = 'btree_gist';
DROP EXTENSION btree_gist;
RESET ROLE;
\echo -- 10e grant the admin CREATE on the database (under SET ROLE owner), retry as the admin
SET ROLE cairn_fv4_owner;
GRANT CREATE ON DATABASE cairn_fv4_noext TO cairn_fv4_admin;
RESET ROLE;
CREATE EXTENSION IF NOT EXISTS btree_gist;
\echo -- 10f additionally grant CREATE on schema public to the admin, retry
SET ROLE cairn_fv4_owner;
GRANT CREATE ON SCHEMA public TO cairn_fv4_admin;
RESET ROLE;
CREATE EXTENSION IF NOT EXISTS btree_gist;
SELECT extname, pg_get_userbyid(extowner) AS ext_owner, extnamespace::regnamespace AS schema FROM pg_extension WHERE extname = 'btree_gist';
\echo -- 10g revoke the temporary CREATE grants again (under SET ROLE owner)
SET ROLE cairn_fv4_owner;
REVOKE CREATE ON SCHEMA public FROM cairn_fv4_admin;
REVOKE CREATE ON DATABASE cairn_fv4_noext FROM cairn_fv4_admin;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
RESET ROLE;
SELECT datacl FROM pg_database WHERE datname = 'cairn_fv4_noext';
SELECT nspacl FROM pg_namespace WHERE nspname = 'public';
\echo -- 10h the extension is still there, and a second IF NOT EXISTS as the admin is a no-op
CREATE EXTENSION IF NOT EXISTS btree_gist;
SELECT extname FROM pg_extension ORDER BY 1;

\echo ### 11 admin (BYPASSRLS, not owner) reads a forced-RLS table with no policy
\c cairn_fv4_noext cairn_fv4_owner
CREATE TABLE public.fv4_t (id int);
INSERT INTO public.fv4_t VALUES (1), (2);
ALTER TABLE public.fv4_t ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fv4_t FORCE ROW LEVEL SECURITY;
GRANT USAGE ON SCHEMA public TO cairn_fv4_admin;
GRANT SELECT ON public.fv4_t TO cairn_fv4_admin;
SELECT count(*) AS owner_sees FROM public.fv4_t;
\c cairn_fv4_noext cairn_fv4_admin
SELECT count(*) AS admin_sees FROM public.fv4_t;
\c cairn_fv4_noext cairn_fv4_owner
DROP TABLE public.fv4_t;

\echo ### 12 drop databases
\c postgres cairn_fv4_admin
\echo -- 12a as the admin directly (expected to fail: not the owner)
DROP DATABASE cairn_fv4_noext WITH (FORCE);
\echo -- 12b under SET ROLE owner
SET ROLE cairn_fv4_owner;
DROP DATABASE cairn_fv4_noext WITH (FORCE);
DROP DATABASE cairn_fv4_fromtmpl WITH (FORCE);
DROP DATABASE cairn_fv4_core WITH (FORCE);
RESET ROLE;
SET ROLE cairn_fv4_dbos;
DROP DATABASE cairn_fv4_dbosdb WITH (FORCE);
RESET ROLE;
SELECT datname FROM pg_database WHERE datname LIKE 'cairn_fv4_%' ORDER BY 1;

\echo ### 13 drop the roles as the admin (it holds ADMIN on the roles it created)
DROP ROLE cairn_fv4_app;
DROP ROLE cairn_fv4_owner;
DROP ROLE cairn_fv4_dbos;
SELECT rolname FROM pg_roles WHERE rolname LIKE 'cairn_fv4_%' ORDER BY 1;
