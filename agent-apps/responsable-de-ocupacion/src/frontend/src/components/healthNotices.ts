// The employee's STATUS notices, in a single place.
//
// v7.170.0. Before, this wording lived inside `Operations.tsx` and was only seen by
// opening that screen: if you did not go in, you never found out that the employee
// was watching nothing. Now there are two surfaces —the bell in the top bar and the
// block in Operaciones— and the text is written ONCE, here.
//
// This being a separate module is not tidiness for its own sake: 7.169.3 fixed a
// notice that said «they waited up to 14 ms {{age}} ago» because the text and whoever
// painted it had ended up on different versions. Two copies of a sentence diverge;
// a single one cannot.
//
// RULE when touching any message here: if its **placeholders** or its **units**
// change, its i18n **key** changes too. In an already generated app the language
// file wins over the `defaultValue`, so keeping the key with different content
// guarantees the old sentence keeps being painted.
import type { TFunction } from 'i18next'

export function healthNoticeText(b: any, t: TFunction): string {
  const p = b.params || {}
  switch (b.key) {
    case 'triggers_off':
      return t('operations.health.triggersOff', 'La ejecución autónoma está en pausa: el empleado no actúa por su cuenta (sigue respondiendo al chat y a los canales).')
    // v7.108.0 (INC-KOM-49) — the switch on with ALL routines paused:
    // the panel announced autonomy and the employee was watching nothing.
    case 'no_active_automations':
      return t('operations.health.noActiveAutomations', { count: p.total, defaultValue: 'La ejecución autónoma está activada pero NO hay ninguna rutina activa ({{count}} en pausa): el empleado no vigila nada por su cuenta. Activa alguna en «Trabajo autónomo».' })
    case 'approvals_stuck':
      return t('operations.health.approvalsStuck', { count: p.count, hours: p.hours, defaultValue: 'Tienes {{count}} acción(es) esperando tu aprobación desde hace más de {{hours}} h.' })
    case 'channels_down':
      return t('operations.health.channelsDown', { count: p.count, names: (p.names || []).join(', '), defaultValue: 'Hay {{count}} canal(es) con problemas de conexión: {{names}}.' })
    case 'connectors_down':
      return t('operations.health.connectorsDown', { count: p.count, defaultValue: '{{count}} conexión(es) necesitan tu atención (autorización caducada o con errores).' })
    // INC-VIG-59 — the employee's database locks up on itself. Before, this only
    // existed as a traceback in the pod log: the operator saw a 500 when turning
    // autonomy off and had no way to relate it to anything.
    case 'db_write_contention':
      return p.locked_errors
        ? t('operations.health.dbWriteLocked', { count: p.locked_errors, defaultValue: 'La base del empleado está saturada: {{count}} operación(es) no se pudieron guardar por bloqueo. Nada se ha perdido a medias, pero si vuelve a pasar avisa a tu administrador.' })
        // v7.169.2 — present and past are phrased differently. `wait_ms_max` is a
        // HISTORICAL maximum, and phrasing it in the present («are waiting») made a
        // peak from hours ago look as if it were happening now. `happening_now` is
        // the only thing that justifies the present tense.
        // NEW KEYS (`…QueueNow` / `…QueueRecent`), and the reason is a real bug: when
        // these notices were rewritten their placeholders changed (`{{age}}`
        // disappeared) and so did the unit (ms → s), but the key stayed the same. In
        // an ALREADY generated app the locale wins over `defaultValue`, so the old
        // sentence kept being painted: «they waited up to 14 ms {{age}} ago» — the
        // placeholder unsubstituted and the seconds labelled as milliseconds. And the
        // i18n of a generated app is SCAFFOLD, so `--fix-invariants` does not repair it.
        //
        // The rule that comes out of this: if a message's placeholders or units
        // change, CHANGE ITS KEY. An old locale stops matching by itself and the
        // `defaultValue` —which travels with the code— is in charge again.
        : p.happening_now
          ? t('operations.health.dbWriteQueueNow', { n: p.queue_waiting, s: Math.round(Number(p.wait_ms_max) / 1000), defaultValue: 'Ahora mismo hay {{n}} escritura(s) esperando turno (la peor espera ha llegado a {{s}} s). No se pierde nada, pero el empleado va lento. Suele ser demasiado trabajo autónomo a la vez.' })
          : t('operations.health.dbWriteQueueRecent', { s: Math.round(Number(p.wait_ms_max) / 1000), min: Math.max(1, Math.round(Number(p.max_wait_seconds_ago || 0) / 60)), defaultValue: 'Hace {{min}} min las escrituras llegaron a esperar {{s}} s. Ahora mismo no hay ninguna en cola: si no se repite, no hay nada que hacer.' })
    case 'platform_degraded':
      return p.reason === 'unreachable'
        ? t('operations.health.platformUnreachable', 'La plataforma no responde ahora mismo: las capacidades extra (skills) y los conectores externos pueden estar limitados temporalmente. Si persiste, avisa a tu administrador.')
        : t('operations.health.platformDegraded', 'Las capacidades extra (skills) y los conectores externos no están disponibles: esta instalación se desplegó con una versión anterior de la plataforma. Pide a tu administrador que actualice el despliegue.')
    default:
      return b.key
  }
}
