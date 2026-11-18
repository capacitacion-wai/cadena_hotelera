-- Profiling module, migration 2 (level 3, «by function and data»): the teams.
-- Additive and idempotent: nothing is dropped or rewritten; an app at level 1-2 gets the table empty.
--
-- perfilado_equipos: one row per team of the app.
--   id      stable slug the data rows store in their `equipo` column (never reused: a team is
--           archived, not deleted, so a new team never inherits an old team's records)
--   activo  1 = in use, 0 = archived (kept: records still point at it)
-- A person belongs to ONE team: `perfilado_usuarios.equipo` (reserved since migration 1) holds its id.
CREATE TABLE IF NOT EXISTS perfilado_equipos (
    id TEXT PRIMARY KEY,
    nombre TEXT NOT NULL UNIQUE,
    activo INTEGER NOT NULL DEFAULT 1,
    creado_por TEXT,
    creado_en TEXT NOT NULL,
    actualizado_en TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_perfilado_equipos_activo ON perfilado_equipos (activo);
