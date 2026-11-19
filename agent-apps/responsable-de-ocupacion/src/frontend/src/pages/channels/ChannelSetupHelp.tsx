/**
 * Step-by-step guide to set up a channel — for someone NON-technical.
 *
 * Why it exists: the wizard asks for a token, a SID or an IMAP host, and whoever fills it
 * in usually does not know where any of the three comes from. The result was a half-done
 * channel, or a customer blocked waiting for someone "from IT" to help.
 *
 * It covers BOTH halves, which is where the gap was: what has to be done in the EXTERNAL
 * system (Telegram, your mail provider, Twilio) and what has to be done here, in WAI.
 * And it states up front the conditions that surprise later: that a Telegram bot cannot
 * write first, that Gmail with two-step verification does NOT accept your normal password,
 * or that WhatsApp only lets you start a conversation with approved templates.
 */
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle,
  Divider, Link, Stack, Step, StepContent, StepLabel, Stepper, Typography,
} from '@mui/material'
import { useTranslation } from 'react-i18next'

export type SetupKind = 'telegram_bot' | 'imap_smtp' | 'twilio' | 'bot_token'

type Paso = { titulo: string; cuerpo: string; enlace?: { texto: string; url: string } }
type Guia = { titulo: string; intro: string; pasos: Paso[]; aviso?: string; enWai: string }

/** The content lives here and not in i18n on purpose: they are long, structured
 *  instructions, and splitting them into 40 loose keys makes them impossible to maintain. */
function guias(t: (k: string, d?: string) => string): Record<SetupKind, Guia> {
  return {
    telegram_bot: {
      titulo: t('channels.help.telegram.title', 'Cómo conectar Telegram'),
      intro: t('channels.help.telegram.intro',
        'Vas a crear un bot de Telegram: una "cuenta" con la que tu empleado recibe y envía mensajes. Se hace desde el propio Telegram y tarda un par de minutos.'),
      pasos: [
        { titulo: 'Abre Telegram y busca @BotFather',
          cuerpo: 'Es la cuenta oficial de Telegram para crear bots. Tiene una marca de verificación azul; si ves varias parecidas, la buena es exactamente "BotFather".' },
        { titulo: 'Escríbele /newbot',
          cuerpo: 'Te preguntará dos cosas: un NOMBRE visible (por ejemplo "Vigía del cluster") y un USUARIO, que debe terminar en "bot" (por ejemplo "vigia_cluster_bot"). Si el usuario ya existe, te pedirá otro.' },
        { titulo: 'Copia el token que te da',
          cuerpo: 'Te responde con una línea larga del estilo 123456789:AAH... Eso es el token. Trátalo como una contraseña: quien lo tenga puede escribir como tu empleado.' },
        { titulo: 'Pégalo aquí, en "Token del bot"',
          cuerpo: 'Con eso el empleado ya puede recibir. Puedes comprobarlo escribiéndole al bot desde tu Telegram.' },
      ],
      aviso: t('channels.help.telegram.warning',
        'IMPORTANTE: un bot de Telegram NO puede escribir primero a nadie. Cada persona que quiera recibir avisos tiene que abrir el bot y pulsar «Iniciar» (o escribirle algo) UNA vez. Hasta entonces, el empleado no tiene forma de contactarla — es una regla de Telegram, no una limitación de WAI.'),
      enWai: t('channels.help.telegram.inWai',
        'Después, en Usuarios: pon el @usuario de Telegram de cada persona en su ficha. Es lo que permite que el empleado sepa quién le escribe, le dé su memoria y sus permisos, y pueda enviarle avisos.'),
    },
    imap_smtp: {
      titulo: t('channels.help.email.title', 'Cómo conectar un correo'),
      intro: t('channels.help.email.intro',
        'Tu empleado necesita su PROPIA cuenta de correo, no la tuya personal: con ella recibe, responde y firma. Puede ser una cuenta nueva del dominio de tu empresa.'),
      pasos: [
        { titulo: 'Crea (o elige) una cuenta para el empleado',
          cuerpo: 'Por ejemplo vigia@tuempresa.com. Que sea suya evita que sus respuestas se mezclen con tu correo personal.' },
        { titulo: 'Si tiene verificación en dos pasos, crea una "contraseña de aplicación"',
          cuerpo: 'Gmail, Outlook y la mayoría de proveedores NO aceptan tu contraseña normal desde programas externos. En los ajustes de seguridad de la cuenta busca "contraseñas de aplicación" y genera una: es una clave de un solo uso para este empleado, y se puede revocar sin cambiar tu contraseña.' },
        { titulo: 'Ten a mano los datos del servidor',
          cuerpo: 'Necesitas el servidor de ENTRADA (IMAP) y el de SALIDA (SMTP) con sus puertos. Para los proveedores conocidos los rellenamos nosotros; si usas un correo propio, te los da quien administra tu dominio.' },
        { titulo: 'Pega correo y contraseña aquí',
          cuerpo: 'Y elige desde cuándo quieres que procese: lo normal es "desde ahora", para que no se ponga a contestar correos viejos.' },
      ],
      enWai: t('channels.help.email.inWai',
        'Después, decide QUIÉN puede escribirle: por dominio (todos los de tuempresa.com) o solo las personas del directorio. Lo que no cumpla la regla, lo ignora en silencio.'),
    },
    twilio: {
      titulo: t('channels.help.whatsapp.title', 'Cómo conectar WhatsApp'),
      intro: t('channels.help.whatsapp.intro',
        'WhatsApp no permite conectar una cuenta personal: hace falta un proveedor autorizado. Usamos Twilio, que es el más extendido.'),
      pasos: [
        { titulo: 'Crea una cuenta en Twilio',
          cuerpo: 'Con la cuenta de prueba (sandbox) puedes validar que todo funciona antes de contratar un número propio.',
          enlace: { texto: 'twilio.com', url: 'https://www.twilio.com' } },
        { titulo: 'Consigue un número de WhatsApp',
          cuerpo: 'En el sandbox te dan uno compartido para pruebas. Para producción hay que solicitar un número propio y que Meta lo apruebe: no es inmediato, cuenta con unos días.' },
        { titulo: 'Copia el Account SID y el Auth Token',
          cuerpo: 'Están en la portada del panel de Twilio, nada más entrar. El Auth Token es una contraseña: no lo compartas por chat ni por correo.' },
        { titulo: 'Pega aquí los tres datos',
          cuerpo: 'El número en formato internacional (+34600111222), el SID y el token.' },
      ],
      aviso: t('channels.help.whatsapp.warning',
        'A tener en cuenta: WhatsApp solo deja responder libremente durante las 24 horas siguientes al último mensaje de la persona. Fuera de esa ventana, iniciar una conversación exige plantillas aprobadas por Meta. Para avisos proactivos frecuentes, Telegram o el correo dan mucha menos guerra.'),
      enWai: t('channels.help.whatsapp.inWai',
        'Después, en Usuarios: pon el teléfono de cada persona en formato +34600111222 en su ficha. Es lo que permite reconocerla cuando escriba y poder avisarla.'),
    },
    bot_token: {
      titulo: t('channels.help.slack.title', 'Cómo conectar Slack'),
      intro: t('channels.help.slack.intro',
        'Vas a crear una "app" de Slack para tu espacio de trabajo y darle permiso de leer y escribir en un canal.'),
      pasos: [
        { titulo: 'Crea una app en Slack',
          cuerpo: 'Entra en el panel de apps de Slack y crea una nueva "from scratch", eligiendo tu espacio de trabajo.',
          enlace: { texto: 'api.slack.com/apps', url: 'https://api.slack.com/apps' } },
        { titulo: 'Dale permisos de bot',
          cuerpo: 'En "OAuth & Permissions" añade los permisos para leer mensajes del canal y publicar en él. Slack los llama scopes.' },
        { titulo: 'Instálala y copia el token',
          cuerpo: 'Al instalarla en el espacio te da un token que empieza por xoxb-. Ese es el que pegas aquí.' },
        { titulo: 'Invita al bot al canal y copia su ID',
          cuerpo: 'En el canal donde quieras que trabaje, escribe /invite y elige la app. El ID del canal está en su enlace o en "Ver detalles del canal".' },
      ],
      enWai: t('channels.help.slack.inWai',
        'Después, elige si responde automáticamente o solo registra lo que llega para que una persona lo revise.'),
    },
  }
}

export default function ChannelSetupHelp(
  { kind, open, onClose }: { kind: SetupKind; open: boolean; onClose: () => void },
) {
  const { t } = useTranslation()
  const g = guias(t as (k: string, d?: string) => string)[kind]
  if (!g) return null
  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{g.titulo}</DialogTitle>
      <DialogContent dividers>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>{g.intro}</Typography>
        <Stepper orientation="vertical" nonLinear activeStep={-1}>
          {g.pasos.map((p, i) => (
            <Step key={i} active expanded>
              <StepLabel>
                <Typography variant="body2" sx={{ fontWeight: 600 }}>{p.titulo}</Typography>
              </StepLabel>
              <StepContent>
                <Typography variant="body2" color="text.secondary">{p.cuerpo}</Typography>
                {p.enlace && (
                  <Link href={p.enlace.url} target="_blank" rel="noopener" variant="body2">
                    {p.enlace.texto}
                  </Link>
                )}
              </StepContent>
            </Step>
          ))}
        </Stepper>
        {g.aviso && <Alert severity="warning" sx={{ mt: 2 }}>{g.aviso}</Alert>}
        <Divider sx={{ my: 2 }} />
        <Box>
          <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
            {t('channels.help.inWaiTitle', 'Y aquí, en WAI')}
          </Typography>
          <Typography variant="body2" color="text.secondary">{g.enWai}</Typography>
        </Box>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t('common.close', 'Cerrar')}</Button>
      </DialogActions>
    </Dialog>
  )
}
