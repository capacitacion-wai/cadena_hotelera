-- Profiling module, migration 3 (level 4, «by organisation»): the outside organisations and the
-- portfolios of the internal people. Additive and idempotent: nothing is dropped or rewritten; an app at
-- level 1-3 gets the tables empty.
--
-- perfilado_organizaciones: one row per outside organisation (a supplier, a customer…).
--   id      stable slug the data rows store in their `organizacion` column (never reused: an
--           organisation is archived, not deleted, so a new one never inherits an old one's records)
--   activo  1 = in use, 0 = archived (kept: records still point at it)
-- A person of an organisation has its id in `perfilado_usuarios.organizacion` (reserved since
-- migration 1); an INTERNAL person has it NULL.
CREATE TABLE IF NOT EXISTS perfilado_organizaciones (
    id TEXT PRIMARY KEY,
    nombre TEXT NOT NULL UNIQUE,
    activo INTEGER NOT NULL DEFAULT 1,
    creado_por TEXT,
    creado_en TEXT NOT NULL,
    actualizado_en TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_perfilado_organizaciones_activo ON perfilado_organizaciones (activo);
-- The PORTFOLIO of an internal person: the organisations they reach. `perfilado_usuarios.con_cartera`
-- says whether they have one (0 = every organisation; 1 = only the rows below, and none if there are
-- none: an empty portfolio sees no organisation — it fails closed).
CREATE TABLE IF NOT EXISTS perfilado_carteras (
    usuario INTEGER NOT NULL,
    organizacion TEXT NOT NULL,
    PRIMARY KEY (usuario, organizacion)
);
CREATE INDEX IF NOT EXISTS ix_perfilado_carteras_organizacion ON perfilado_carteras (organizacion);
ALTER TABLE perfilado_usuarios ADD COLUMN con_cartera INTEGER NOT NULL DEFAULT 0;
