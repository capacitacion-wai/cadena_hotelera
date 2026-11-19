/**
 * The application on a phone: only the chat with its workspace, full screen.
 *
 * Why it is not the usual `Layout` with smaller things: the employee's menu is a fixed-width
 * `permanent` `Drawer`. On a narrow screen it either eats the content or leaves tables,
 * details and approvals unreadable. Serving an application that can be RENDERED but not USED
 * is worse than not serving it: the user thinks it is broken.
 *
 * So on mobile we serve the part that does work on a phone —talking to the employee, which
 * is also what people do on the way somewhere— and say, once and without getting in the way,
 * that the rest is seen from a computer. It is said instead of hidden because a menu that
 * disappears without explanation reads as a bug or as a loss of permissions.
 *
 * The notice is remembered per device (`localStorage`): it informs the first time and does
 * not get in the way again. If there is no `localStorage` (private mode), it is shown and
 * that is it — bothering too much is better than crashing over storage the browser does not give.
 */
import { useState, type ReactNode } from 'react';
import { Alert, Box, Collapse, IconButton } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { useTranslation } from 'react-i18next';

const VISTO = 'wai.movil.aviso_visto';

function yaLoVio(): boolean {
  try {
    return localStorage.getItem(VISTO) === '1';
  } catch {
    return false;
  }
}

export function SoloChatMovil({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const [visible, setVisible] = useState(() => !yaLoVio());

  const cerrar = () => {
    setVisible(false);
    try {
      localStorage.setItem(VISTO, '1');
    } catch {
      /* private mode: it will be shown again, which is better than failing */
    }
  };

  return (
    <Box
      sx={{
        // `100dvh` and not `100vh`: on a phone the browser bar appears and disappears on
        // scroll, and with `vh` the chat composer ends up below the visible edge — the user
        // sees the keyboard and does not see where to type.
        height: '100dvh',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        bgcolor: 'background.default',
      }}
    >
      <Collapse in={visible} unmountOnExit>
        <Alert
          severity="info"
          sx={{ borderRadius: 0, py: 0.5, alignItems: 'center' }}
          action={(
            <IconButton
              size="small"
              onClick={cerrar}
              aria-label={t('movil.aviso.cerrar', 'Entendido') as string}
            >
              <CloseIcon fontSize="small" />
            </IconButton>
          )}
        >
          {t(
            'movil.aviso.texto',
            'Desde el móvil puedes hablar con tu empleado. El resto de la aplicación se ve desde un ordenador.',
          )}
        </Alert>
      </Collapse>

      {/* `minHeight: 0` is what lets the child scroll inside a flex column.
          Without it, the chat grows until it overflows and the composer goes off screen. */}
      <Box sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        {children}
      </Box>
    </Box>
  );
}

export default SoloChatMovil;
