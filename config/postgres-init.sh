#!/usr/bin/env bash
# Official postgres entrypoint sources this file after initdb and DB creation.
# Its file_env has already read the mounted secret as root, before dropping to
# the postgres OS user. A Compose bind-backed secret may remain owned by UID
# 1000/mode 600, so the server reads a private temporary copy, not that mount.
# This subshell prevents traps/options from changing the official entrypoint.
(
  set -Eeuo pipefail
  umask 077
  task_init_password="$PGDATA/.aeternitas-init-password"
  # Refuse to overwrite an unexpected leftover or symlink after an interrupted
  # initialization. Operators retain that volume and inspect it explicitly.
  set -o noclobber
  printf '%s' "$POSTGRES_PASSWORD" > "$task_init_password"
  trap 'rm -f -- "$task_init_password"' EXIT
  psql --no-psqlrc --no-password --set=ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
DO $aeternitas_init$
BEGIN
  BEGIN
    EXECUTE format(
      'CREATE ROLE aeternitas LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
      pg_read_file(current_setting('data_directory') || '/.aeternitas-init-password')
    );
    EXECUTE format('ALTER DATABASE %I OWNER TO aeternitas', current_database());
    -- Bootstrap identity cannot be demoted. Disable its TCP password instead;
    -- the administrator retains container-local Unix-socket access.
    ALTER ROLE postgres PASSWORD NULL;
  EXCEPTION WHEN OTHERS THEN
    -- Never include SQLERRM/PG_EXCEPTION_CONTEXT: a failed dynamic CREATE ROLE
    -- could contain the supplied password. A fresh error has only fixed text.
    RAISE EXCEPTION 'Aeternitas application role initialization failed; private diagnostics withheld';
  END;
END
$aeternitas_init$;
SQL
)
