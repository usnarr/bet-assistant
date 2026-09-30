-- F15.7 base backup role. The image runs this file once, when it creates the database.
-- The role can only stream a base backup (REPLICATION). It gets no table privilege.
-- The password comes from the Compose secret file and never enters the repository.
\set ON_ERROR_STOP on
\set backup_password `cat /run/secrets/postgres_backup_password`
CREATE ROLE tennis_backup WITH LOGIN REPLICATION PASSWORD :'backup_password';
