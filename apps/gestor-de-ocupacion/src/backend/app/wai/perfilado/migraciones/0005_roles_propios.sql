-- Profiling module, migration 5: custom roles (setting «roles_personalizables», levels 2-4) and
-- the log of every change to them. Additive and idempotent: the reserved table of migration 1 gains
-- its columns (a re-run ADD COLUMN is tolerated by the runner); nothing is dropped or rewritten.
--
-- perfilado_roles_propios: one row per role an administrator composed IN THE APP from the
-- permissions the Definition declares (never another capability or action: `roles_propios.guardar`).
--   id        `propio_<slug>`: never one of the Definition's role ids, never reused
--   permisos  JSON {capability: [actions]}         alcances  JSON {capability: {action: scope}}
--   quien     the one line «who it is for»          ambito    interno | organizacion (level 4)
ALTER TABLE perfilado_roles_propios ADD COLUMN quien TEXT;
ALTER TABLE perfilado_roles_propios ADD COLUMN ambito TEXT NOT NULL DEFAULT 'interno';
ALTER TABLE perfilado_roles_propios ADD COLUMN alcances TEXT NOT NULL DEFAULT '{}';
ALTER TABLE perfilado_roles_propios ADD COLUMN cambiado_por TEXT;
ALTER TABLE perfilado_roles_propios ADD COLUMN cambiado_en TEXT;
-- perfilado_roles_propios_historial: who changed which custom role, when and what (kept after the
-- role is deleted).
--   accion   creado | cambiado | borrado
--   detalle  JSON: the role as it was left (and, on a change, `antes`; on a delete, who was moved where)
CREATE TABLE IF NOT EXISTS perfilado_roles_propios_historial (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rol TEXT NOT NULL,
    accion TEXT NOT NULL,
    por TEXT,
    cuando TEXT NOT NULL,
    detalle TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_perfilado_roles_propios_historial_rol ON perfilado_roles_propios_historial (rol);
