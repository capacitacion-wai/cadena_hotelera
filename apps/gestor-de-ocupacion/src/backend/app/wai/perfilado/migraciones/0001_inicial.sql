-- Profiling module, migration 1: the user records and the (reserved) custom roles.
-- Idempotent: it runs as an upgrade and as a fresh install alike.
--
-- perfilado_usuarios: one row per person (email = the platform identity, lower case).
--   roles         JSON list of role ids (permisos.json → roles[].id)
--   activo        1 = can use the app, 0 = deactivated (kept for history, never deleted)
--   origen        proyecto | correo | despliegue | pruebas
--   equipo        RESERVED for level 3 (scope «team»): NULL until then
--   organizacion  RESERVED for level 4 (external organisations): NULL until then
CREATE TABLE IF NOT EXISTS perfilado_usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    nombre TEXT,
    roles TEXT NOT NULL DEFAULT '[]',
    activo INTEGER NOT NULL DEFAULT 1,
    origen TEXT NOT NULL DEFAULT 'correo',
    equipo TEXT,
    organizacion TEXT,
    creado_por TEXT,
    creado_en TEXT NOT NULL,
    actualizado_en TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_perfilado_usuarios_activo ON perfilado_usuarios (activo);
CREATE INDEX IF NOT EXISTS ix_perfilado_usuarios_equipo ON perfilado_usuarios (equipo);
CREATE INDEX IF NOT EXISTS ix_perfilado_usuarios_organizacion ON perfilado_usuarios (organizacion);
-- RESERVED (setting «roles_personalizables», levels 2-4): roles an administrator composes from the
-- permissions DECLARED in the Definition. Disabled in this version: nothing reads or writes it.
CREATE TABLE IF NOT EXISTS perfilado_roles_propios (
    id TEXT PRIMARY KEY,
    nombre TEXT NOT NULL,
    permisos TEXT NOT NULL DEFAULT '{}',
    creado_por TEXT,
    creado_en TEXT NOT NULL
);
