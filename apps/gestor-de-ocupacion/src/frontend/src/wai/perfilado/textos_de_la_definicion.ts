/**
 * Profiling module — the Definition's texts in EACH language of the app (SCAFFOLD: yours to fill, never overwritten).
 *
 * The task and profile names come from the Definition in its primary language; the screens show them through these
 * keys, per language code (the app's i18n codes: `es`, `en`…):
 *
 *   def_tarea_<task id>_corto   the task's SHORT name (2-6 words) — in EVERY language, the primary one too: it is the
 *                               row of «What each one can do»; without it the sentence is cut
 *   def_tarea_<task id>         the whole sentence (`que`)            — in every language but the primary one
 *   def_rol_<profile id>        the profile's name                    — in every language but the primary one
 *   def_rol_<profile id>_quien  who the profile is for (if it has one) — in every language but the primary one
 *   def_organizaciones          the setting `nombre_organizacion`     — in every language but the primary one
 *
 * `modulos.py comprobar` FAILS while one is missing and names it. Keep the shape: after the `=` there is ONLY a JSON
 * object (double quotes, no comments, no trailing commas) — the verifier reads it as JSON.
 */
export const TEXTOS_DE_LA_DEFINICION: Record<string, Record<string, string>> = {
  "es": {
    "def_tarea_CAP-1_corto": "Ver ocupación y reservas",
    "def_tarea_CAP-2_corto": "Proponer una reubicación",
    "def_tarea_CAP-3_corto": "Aprobar movimientos",
    "def_tarea_CAP-4_corto": "Cargar datos de ejemplo"
  }
}
