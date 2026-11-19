/**
 * Strings that belong to the SKILL, not to the employee (v12.5.0). INVARIANT.
 *
 * THE PROBLEM IT SOLVES. The pages (`pages/Agent.tsx`, `Operations.tsx`…) are
 * INVARIANTS: they travel with every upgrade. Their strings lived in `es.ts`, which is the
 * employee's SCAFFOLD and which `--fix-invariants` does NOT touch — on purpose, because each
 * employee customises it. Result: every time the skill added a new key, when an employee
 * was upgraded its `es.ts` did not have it, verifier §8.5 failed and someone had to copy
 * it by hand in both languages. It happened in 12.2.x and again in 12.4.0. A rule that
 * demands a manual step on every upgrade is not a rule: it is a scheduled oversight.
 *
 * HOW IT WORKS. This file is the BASE layer and `es.ts` is merged ON TOP (see `i18n.ts`), so:
 *
 *   · a new skill key reaches EVERY upgraded employee ON ITS OWN —this file is
 *     invariant—, without copying anything;
 *   · the employee keeps the power to override any string: if it defines it in its `es.ts`,
 *     its own wins. That is what keeps the base from taking customisation away.
 *
 * WHERE EACH STRING GOES, and there is no third option:
 *
 *   · it is used by a skill file (any except `App.tsx` and `utils/dataLabels.ts`) → HERE;
 *   · it belongs to the employee's domain (names of its screens, its business) → in `es.ts`.
 *
 * A gate in the monorepo `pre-commit` checks it: a NEW key in `es.ts` used by a skill file
 * is rejected, because it would not reach the employees and nobody would notice —
 * on screen it would fall back to the default text (in Spanish) and only English would give it away.
 *
 * Keys ALREADY in every employee's `es.ts` need not be moved: the base only has to cover
 * what they lack. The ones from 12.2.0 onwards live here.
 */
export default {
  voz: {
    perfil: {
      salida: 'Por dónde suena',
      altavoz: 'Por el altavoz',
      auricular: 'Por el auricular',
      salidaAyuda: 'En el móvil. Por altavoz es lo que se espera al ver la pantalla; por auricular, si te lo pones en la oreja como una llamada.',
      sonando: 'sonando…',
      titulo: 'Su voz',
      ayuda: 'Cómo suena cuando alguien habla con él desde el chat. Es parte de su identidad, así que lo mismo para todo el mundo.',
      voz: 'Voz',
      estilo: 'Cómo habla',
      estiloEj: 'Por ejemplo: cercano y tranquilo. O: seco y ejecutivo, al grano.',
      estiloAyuda: 'Una línea. Cambia el tono, no lo que dice: las cifras, los nombres y las fechas nunca se tocan.',
      acompana: 'Mientras consulta algo',
      acompanaAyuda: 'Puede tardar dos minutos. Cada frase que dice mientras espera es un turno que se paga, así que hay empleados a los que les conviene callar.',
      acompanaNada: 'Calla mientras trabaja',
      acompanaHechos: 'Cuenta lo que va haciendo',
      acompanaTodo: 'Cuenta todo, incluso lo que va pensando',
      arranque: 'Al abrir la voz',
      arranqueAyuda: '«Solo mientras se pulsa» es lo único que garantiza que no se oye a nadie más. En una oficina abierta no es opcional.',
      arranqueAbierto: 'Escucha en cuanto se abre',
      arranquePulsar: 'Solo mientras se pulsa el espacio',
      guardado: 'Guardado.',
      error: 'No se pudo guardar.',
    },
    salaRuidosa: 'bastante ruido alrededor',
    salaSilenciosa: 'poco ruido alrededor',
    ajustandoMicro: 'Ajustando el micrófono · no te escucha',
    calibrando: 'Escuchando la sala un momento…',
    ajustar: 'Ajustar el micrófono',
    ajustarMedido: 'Ajustar el micrófono · {{f}}',
    cal: {
      titulo: 'Ajustar el micrófono',
      ayuda: 'Di algo: si la barra se mueve, te oye.\n\n«Medir la sala» escucha dos segundos el ruido que tienes alrededor. Sirve para que no te corte cuando hagas una pausa, ni te conteste a un ruido.',
      porDefecto: 'El micrófono por defecto',
      sinNombre: 'Micrófono sin nombre',
      nivel: 'Lo que le está llegando',
      teOigo: 'Te oigo.',
      nada: 'No me llega nada todavía.',
      sinMicro: 'No he podido abrir el micrófono: {{err}}',
      resultado: 'Ruido alrededor: {{fondo}}',
      silenciosa: 'Hay poco ruido. Te contestará en cuanto termines de hablar.',
      ruidosa: 'Hay bastante ruido. Esperará un poco más para no cortarte.',
      cancelar: 'Cancelar',
      medir: 'Medir la sala',
      midiendo: 'Midiendo…',
      hablar: 'Hablar',
    },
    registroVacio: 'Todavía no ha pasado nada.',
    instruccionAvanceReal: 'Esto es lo que estás haciendo AHORA MISMO, y puede venir en inglés: cuéntalo en español, en UNA frase corta, en primera persona y sin listas, como quien va informando sin interrumpir. NO DIGAS CÓMO VA: ni que estás esperando, ni que aún no tienes nada, ni que no te ha llegado, ni que va a salir bien — eso no lo sabes desde aquí y casi siempre es mentira. Di solo lo que pone abajo, sin leer coordenadas, enlaces ni nombres de herramientas, sin anticipar la respuesta final y sin seguir hablando después:\n\n«{{x}}»',
    tarjetaRuta: 'un mapa con la ruta',
    tarjetasRuta: '{{n}} mapas con las rutas',
    tarjetasLugar: '{{n}} mapas con las direcciones',
    tarjetaLugar: 'un mapa con la dirección',
    hayEnPantalla: 'Además, en el chat le has dejado {{que}}.',
    enOtraPestana: 'Estás en otra pestaña · no te escucha',
    estoyMirando: 'Estoy mirando en {{donde}}.',
    yaEstoyEnUna: 'Sigo con la consulta anterior. Dile que esperes un momento y no vuelvas a preguntar hasta que llegue.',
    registro: 'Qué está pasando por dentro',
    copiar: 'Copiar todo',
    copiado: 'Copiado',
    enPausa: 'En pausa · no te escucha',
    trabajando: 'Está en ello…',
    hablando: 'Hablando…',
    volverAlChat: 'Volver al chat',
    instruccionAvance: 'Di EXACTAMENTE esto, sin añadir ni quitar nada y sin seguir hablando después: «{{x}}»',
    instruccionLeer: 'Lee esto en alto para alguien que lo ESCUCHA, no lo lee.',
    instruccionSesion: 'Eres la VOZ de este empleado. No decides nada y no consultas nada: solo dices en alto lo que te dan. Nunca respondas por tu cuenta ni inventes.',
    teOye: 'Te está oyendo…',
    pulsaEspacio: 'Mantén pulsado el espacio para hablar',
    titulo: 'Hablar',
    subtitulo: 'Conversación hablada con tu empleado. Él oye, piensa y te contesta en voz alta.',
    listo: 'Listo',
    empezar: 'Empezar a hablar',
    pausar: 'Dejar de escuchar',
    seguir: 'Seguir hablando',
    parar: 'Parar',
    modoNormal: 'Normal: él decide cuándo has terminado',
    modoPulsar: 'Solo mientras pulso espacio',
    vacio: 'Pulsa «Empezar a hablar», da permiso al micrófono y háblale con normalidad.',
    tu: 'Tú',
    el: 'Él',
    avance: 'mientras espera',
    traza: 'Qué está pasando por dentro',
    pidiendoMicro: 'Pidiendo permiso del micrófono…',
    sinMicro: 'Sin micrófono: {{err}}',
    conectando: 'Conectando…',
    sinCredencial: 'No he conseguido credencial de voz',
    escuchando: 'Escuchando · habla cuando quieras',
    rechazada: 'La conexión de voz fue rechazada',
    sinRespuesta: 'No he podido consultarlo.',
    errorConsulta: 'Me ha dado un error al consultarlo.',
    diExacto: 'Di EXACTAMENTE esto, sin añadir ni quitar nada, sin saludar y sin seguir hablando después: «{{frase}}»',
    herramientaDesc: 'Le pide algo al empleado. TODO lo que tenga que ver con lo que él hace pasa por aquí, aunque creas saberte la respuesta. Tarda entre 20 segundos y dos minutos.',
    herramientaArg: 'La petición, clara y completa, en español.',
  },
  operations: {
    timeLimits: {
      title: 'Tiempo máximo de trabajo',
      hint: 'Cuánto puede trabajar el empleado en una sola tarea antes de detenerla. Al llegar al máximo, la detiene y lo dice. Súbelo si hace consultas largas; un máximo alto también deja que una tarea atascada gaste más.',
      chat: 'Máx. minutos por respuesta en el chat',
      chatHelp: 'Lo que espera una persona a la respuesta.',
      auto: 'Máx. minutos por tarea autónoma',
      autoHelp: 'Rutinas, automatizaciones y respuestas por los canales.',
      range: 'Entre {{min}} y {{max}} minutos.',
    },
    permsSummary: {
      no_writes: 'Usa sus herramientas de consulta sin pedir permiso; no tiene herramientas que escriban en sistemas externos.',
      read_only: 'Usa sus herramientas de consulta sin pedir permiso; no puede escribir en ningún sistema externo.',
      all_approval: 'Consulta sin pedir permiso; toda escritura en un sistema externo espera tu aprobación.',
      all_free: 'Usa cualquier herramienta, incluidas las escrituras, sin pedir confirmación.',
      mixed: 'Consulta sin pedir permiso; unas escrituras las hace solo y otras esperan tu aprobación o no las puede hacer.',
      unreadable: 'No se ha podido leer la tabla de permisos de la plataforma.',
      ownRules_one: '{{count}} con regla propia.',
      ownRules_other: '{{count}} con regla propia.',
    },
    automations: {
      evals: {
        summaryErrors_one: ' y {{count}} no pudo comprobarla',
        summaryErrors_other: ' y {{count}} no pudo comprobarlas',
      },
    },
  },
  welcome: {
    member: {
      whatICanDo: 'Qué puedo hacer por ti',
      talk: 'Hablar con {{nombre}}',
    },
  },
  dashboard: {
    indicatorFailed: 'No se pudo calcular: su consulta tiene un fallo. Pídele al agente de código que la revise.',
    trendFailed: 'No se pudo dibujar: su consulta tiene un fallo. Pídele al agente de código que la revise.',
  },
  errors: {
    api: {
      agent_starting: 'Tu empleado está arrancando y su agente todavía no atiende. Espera unos segundos y vuelve a enviar el mensaje.',
      conversation_not_found: 'Esa conversación no existe.',
      location_request_gone: 'Esa petición de ubicación ya no está esperando.',
      only_own_conversations: 'Solo puedes reiniciar tus propias conversaciones.',
    },
  },
  approvals: {
    detail: {
      willSend: 'Lo que se enviará',
    },
  },
  consumo: {
    voz: 'Hablar',
    vozHint: 'Conversaciones por voz. Se paga aparte porque el audio cuesta unas veinte veces más que el texto.',
    escribiendo: 'escribiendo',
    hablando: 'hablando',
  },
  agent: {
    chat: {
      agentRestarting: 'El empleado se está actualizando para usar un conector que acabas de conectar. Vuelve a intentarlo en un minuto.',
      latido: {
        texto: 'Sigo trabajando · {{que}} ({{min}} min)',
        consultando: 'consultando {{donde}}',
        herramienta: 'esperando el resultado de un paso',
        pensando: 'pensando',
      },
      turnInProgress: 'Sigo con tu mensaje anterior. Te he dejado el nuevo en la caja: envíalo cuando termine o pulsa Detener.',
      turnTimeLimit: 'He detenido esta tarea porque superó el tiempo máximo de respuesta ({{min}} min). Un administrador del empleado puede subirlo en Comportamiento; también puedes dividir la tarea en partes.',
      location: {
        // v12.2.0 — the "share when it asks" toggle of the attach menu.
        autoOn: 'Compartirla cuando la pida',
        autoOff: 'Dejar de compartirla cuando la pida',
      },
      compact: {
        // v12.4.0 — the fold of what came before a compaction.
        showCompacted: '{{n}} mensajes anteriores, resumidos al compactar',
        showCompactedOne: 'Un mensaje anterior, resumido al compactar',
        hideEarlier: 'Ocultar los mensajes anteriores',
      },
      // 12.22.0 — how much the employee thinks on THIS message. The level names
      // come from the server (they are the employee's); here only what surrounds them.
      // 12.28.0 — how the turn was resolved, when it did NOT go the normal way.
      resuelto: {
        atajo: 'Directo · {{c}}',
        atajoAyuda: 'Su definición declara que esta capacidad se resuelve con una sola llamada, así que se contestó sin arrancar al agente: ni razonó, ni reenvió la conversación, ni miró el catálogo de herramientas.',
        contesto: 'Respondido en la conversación',
        pregunto: 'Te ha preguntado antes de trabajar',
        recepcionAyuda: 'Esto no necesitaba trabajo —una cortesía, algo general, o faltaba un dato—, así que se atendió en la conversación sin arrancar al agente.',
      },
      razonamiento: {
        tooltip: 'Cuánto piensa antes de contestar',
        titulo: 'Cuánto piensa en este mensaje',
        delEmpleado: 'El del empleado',
        delEmpleadoAyuda: 'Lo que tenga configurado. No eliges por él.',
        delEmpleadoCon: 'El del empleado ({{nivel}})',
        delEmpleadoSinFijar: 'Él tampoco lo ha fijado, así que lo pone el modelo.',
        delEmpleadoSinFijarCon: 'Él tampoco lo ha fijado, así que lo pone el modelo: hoy sale {{nivel}}.',
        fijar: 'Dejarlo como mi nivel por defecto',
        fijarAyuda: 'Se aplicará a tus próximos mensajes, aquí y en el móvil.',
        yaFijado: 'Ya es el tuyo por defecto.',
      },
    },
  },
  // v12.7.0 — WHO requested each run and WHICH WAY it came in.
  runs: {
    // A tool call's state on a run's card (15.3.1): a failure is not «pending».
    tools: {
      failed: 'Falló',
      interrupted: 'Sin terminar',
      running: 'En curso',
    },
    cols: {
      requester: 'Quién',
      surface: 'Por dónde',
    },
    request: {
      requester: 'Quién lo pidió',
      surface: 'Por dónde',
    },
    requester: {
      // Nobody: the employee acted on its own. It is an answer, not a gap.
      agent: 'El empleado',
    },
    surface: {
      chat: 'Chat',
      channel: 'Canal',
      automation: 'Automatización',
      watcher: 'Vigilancia',
      scheduled: 'Agenda',
      colleague: 'Compañero',
    },
    // Channel types. The type name says more than a bare "Channel": email and WhatsApp
    // are not handled alike, and an auditor wants to know which door it came in through.
    channelKind: {
      email: 'Correo',
      whatsapp: 'WhatsApp',
      slack: 'Slack',
      telegram: 'Telegram',
    },
  },
  // 14.8.0 — the employee's connector permissions (Operations → Conectores). The SAME words
  // as the platform's panel (docs/permisos-de-conectores.md): the employee inherits its
  // agent or tightens it, never more. The `errores` are the platform's rejection codes.
  // 14.27.0 — who connected or disconnected each connector, and when (kept by the platform).
  historialDeConexiones: { titulo: "Historial de conexiones", vacio: "Aún no hay conexiones apuntadas.", sinPlataforma: "No se ha podido leer el historial de la plataforma.", cuando: "Cuándo", conector: "Conector", accion: "Qué", quien: "Quién", cuenta: "Cuenta", resultado: "Resultado", ok: "Bien", error: "Falló",
    acciones: { connect: "Conectó", change_account: "Cambió de cuenta", authorize: "Empezó a autorizar", activate: "Activó", disconnect: "Desconectó" } },
  permisos: {
    titulo: 'Permisos de los conectores',
    intro: 'El empleado usa los permisos de su agente. Puedes dejarlos igual o hacerlos más estrictos, nunca darle más de lo que tiene el agente: lo que el agente permite es lo que se ha probado.',
    hereda: 'Hereda del agente',
    personalizar: 'Personalizar',
    heredaAyuda: 'Usa exactamente los permisos de su agente.',
    personalizarAyuda: 'Igual o más estricto que el agente, conector a conector.',
    potestad: 'Por defecto',
    delAgente: 'En el agente: {{valor}}',
    noProbado: 'no se ha probado así en el agente',
    potestades: {
      inherit: 'Hereda del agente',
      write: 'Escribe',
      write_with_approval: 'Con aprobación',
      read_only: 'Solo lectura',
    },
    modos: {
      inherit: 'Hereda',
      write: 'Escribe',
      write_with_approval: 'Con aprobación',
      read_only: 'Solo lectura',
      custom: 'Personalizado',
    },
    reglas: {
      allow: 'Permitir',
      ask: 'Pedir aprobación',
      deny: 'Denegar',
    },
    efectivo: {
      allow: 'Puede',
      ask: 'Con aprobación',
      deny: 'No puede',
    },
    origen: {
      read: 'Lee',
      inherited_agent: 'Heredado del agente',
      connector_mode: 'Modo del conector en el agente',
      custom: 'Regla del agente',
      employee: 'Regla del empleado',
      platform: 'De la plataforma',
    },
    io: {
      read: 'Lee',
      write: 'Escribe',
      sinClasificar: 'Sin clasificar',
      unclassified: 'Sin clasificar: cuenta como escritura',
    },
    columnas: {
      herramienta: 'Herramienta',
      tipo: 'Tipo',
      agente: 'En el agente',
      regla: 'Regla del empleado',
      efectivo: 'Resultado',
    },
    abreMas: 'Abre más que la potestad del agente',
    sinRegla: 'Sin regla (hereda)',
    motivo: 'Motivo',
    motivoEj: '¿Por qué esta excepción?',
    verHerramientas: 'Ver herramientas ({{count}})',
    ocultar: 'Ocultar herramientas',
    sinHerramientas: 'El catálogo de este conector no tiene herramientas habilitadas.',
    guardar: 'Guardar',
    descartar: 'Descartar',
    guardado: 'Permisos guardados.',
    ensayando: 'Comprobando con la plataforma…',
    sinTabla: 'No se han podido leer los permisos de la plataforma. Mientras tanto, el empleado no usa ningún conector.',
    tablaVieja: 'No se ha podido contactar con la plataforma: se muestran los últimos permisos conocidos, que son los que se siguen aplicando.',
    convertida: 'Estos permisos vienen de la configuración anterior del empleado. Revísalos y guarda para pasarlos al modelo nuevo.',
    soloAdmin: 'Solo un administrador puede cambiar los permisos.',
    vacio: 'Este empleado no tiene conectores.',
    errores: {
      PERMISOS_EN_CONECTOR_QUE_HEREDA: 'Este conector no está en «Personalizado»: sus herramientas siguen su potestad. Pásalo a Personalizado para marcar excepciones.',
      PERMISO_SIN_CAMBIO: 'Esa excepción vale lo mismo que lo heredado. Quítala: en Personalizado solo se guarda lo que cambia.',
      HERRAMIENTA_DESCONOCIDA: 'Esa herramienta no está en el catálogo del conector.',
      CONECTOR_DESCONOCIDO: 'Ese conector no está en este agente.',
      EMPLEADO_ABRE_MAS: 'Un empleado no puede tener más permisos que su agente. Elige igual o más estricto, o cambia el agente.',
      CLASIFICACION_FUERA_DEL_CATALOGO: 'Si una herramienta lee o escribe se decide en el catálogo del conector, no en el agente.',
      CAMPO_RETIRADO: 'Esta forma de guardar permisos ya no existe. Recarga la página.',
      VALOR_NO_VALIDO: 'Valor no válido.',
      PLATAFORMA_NO_DISPONIBLE: 'No se ha podido contactar con la plataforma. Inténtalo de nuevo en un momento.',
    },
  },
}
