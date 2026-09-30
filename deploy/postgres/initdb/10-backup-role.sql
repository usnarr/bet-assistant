-- F15.7 base backup role. The image runs this file once, when it creates the database.
-- The role can only stream a base backup (REPLICATION). It gets no table privilege.
-- The password comes from the Compose secret file and never enters the repository.
-- F15.5: `tennis-ops provision-roles` (the `db-roles` service) then sets the same password
-- again and checks that the role has no table privilege. The line end (LF or CRLF) is
-- not part of the password.
\set ON_ERROR_STOP on
\set backup_password `tr -d '\r\n' < /run/secrets/postgres_backup_password`
CREATE ROLE tennis_backup WITH LOGIN REPLICATION PASSWORD :'backup_password';
