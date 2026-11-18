-- Profiling module, migration 4: adopting the users an app ALREADY had (modulos.py adoptar).
-- Additive and idempotent: nothing is dropped or rewritten.
--
-- perfilado_adopciones: one row per old user table imported into perfilado_usuarios (once).
--   tabla         the app's old user table (never dropped: it keeps the business data)
--   filas         how many people were imported (origin «adopcion» in perfilado_usuarios)
--   importada_en  when
-- perfilado_usuarios.origen gains the value «adopcion» (a TEXT column: no schema change).
CREATE TABLE IF NOT EXISTS perfilado_adopciones (
    tabla TEXT PRIMARY KEY,
    filas INTEGER NOT NULL DEFAULT 0,
    importada_en TEXT NOT NULL
);
