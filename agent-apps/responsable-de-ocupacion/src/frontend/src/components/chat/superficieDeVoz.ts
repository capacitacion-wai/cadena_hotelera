/**
 * WHAT THIS SURFACE HAS. Only that.
 *
 * The server cannot know it: a mobile browser is not the app —it has no native camera
 * and no notifications— and a denied permission cannot be seen from outside. So it is
 * DECLARED from here, just as the native app declares its own, and the server composes the
 * instructions and returns the functions this surface will have to resolve.
 *
 * And ONLY what can be resolved is declared. A function the client cannot serve leaves
 * the conversation waiting for an answer that never comes, which is worse than not having
 * it: the speaker goes silent and the person does not know why.
 *
 * NOTHING IS RESOLVED HERE ANY MORE, and that is the fix of 20/09/2026. This file also had
 * the functions that read the device, and each one carried its own conversation gates in
 * front: «has it been tried already?», «has the person been asked out loud?». That is now
 * carried by the machine (`conversacion/maquina.ts`), which is where the tests are — and
 * having it in both places was not redundancy but contradiction: the machine asked, the
 * person said yes, and when going to read the device this answered «you have not asked
 * yet». Reading the device is now done by the chat, which is who has to answer the
 * employee.
 */

/** What a browser can really do. No camera and no notifications: on a laptop the «photo
 *  of the moment» opens a file picker, and promising it here costs whoever asks for it a
 *  round trip. Mobile declares its own in its own file. */
export async function capacidadesDelNavegador(): Promise<string[]> {
  const c = ['pantalla']
  // THE LOCATION IS DECLARED ONLY IF IT CAN REALLY BE REQUESTED, and that includes checking
  // the permission. With `geolocation` present but BLOCKED for this page, declaring it is
  // promising something that does not exist: the conversationalist requests it, is told
  // there is no way, and gets stuck repeating that the browser does not share the position
  // — measured on 20/09/2026, even AFTER the person dictated their address out loud.
  //
  // Without declaring it, the server tells it «you don't have that here, ASK for it by
  // voice», which is what has to be done. Removing the option beats asking it not to use it.
  if (typeof navigator !== 'undefined' && navigator.geolocation) {
    let bloqueada = false
    try {
      const p = await navigator.permissions?.query({ name: 'geolocation' as PermissionName })
      bloqueada = p?.state === 'denied'
    } catch { bloqueada = false }   // not knowing is no reason to remove it
    if (!bloqueada) c.push('ubicacion')
  }
  if (typeof document !== 'undefined') c.push('ficheros')
  return c
}
