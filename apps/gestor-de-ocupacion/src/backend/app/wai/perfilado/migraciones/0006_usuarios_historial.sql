-- Profiling module, migration 6: the log of the people DELETED from the app (1.4.1, «Borrar»).
-- Additive and idempotent: nothing is dropped or rewritten.
--
-- perfilado_usuarios_historial: who deleted whom and when (kept after the record is gone).
--   email    the deleted person's email (lower case): their business rows keep it as `dueno`, and while
--            nobody is registered again with it those rows count as «without an owner» (servicio.sin_dueno)
--   accion   borrado
--   detalle  JSON: the record as it was (name, roles, team, organisation, portfolio)
CREATE TABLE IF NOT EXISTS perfilado_usuarios_historial (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    accion TEXT NOT NULL,
    por TEXT,
    cuando TEXT NOT NULL,
    detalle TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_perfilado_usuarios_historial_email ON perfilado_usuarios_historial (email);
