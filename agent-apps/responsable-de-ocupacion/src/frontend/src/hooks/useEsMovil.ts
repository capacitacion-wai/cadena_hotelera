/**
 * Is the user coming in from a phone?
 *
 * It is decided by the WINDOW WIDTH, not by the `user-agent`, and that is not a whim: the
 * user-agent lies in both directions —tablets that declare themselves desktop, desktops in
 * responsive mode that declare themselves mobile— and it also notices nothing after the
 * load. With the width, rotating the phone, splitting the screen or opening the app in a
 * narrow window behave as what they are, with no device lists to maintain.
 *
 * The cut-off is the theme's `sm` (600 px by default in MUI): below it the employee's side
 * menu does not fit, since it is `permanent` and fixed-width, so the full application
 * cannot be used even if it renders.
 */
import { useMediaQuery, useTheme } from '@mui/material';

export function useEsMovil(): boolean {
  const theme = useTheme();
  // `noSsr` because this app is served already mounted: without it, the first render assumes
  // «not mobile» and the full menu flickers before being corrected — exactly on the screen
  // where we wanted it never to appear.
  return useMediaQuery(theme.breakpoints.down('sm'), { noSsr: true });
}

export default useEsMovil;
