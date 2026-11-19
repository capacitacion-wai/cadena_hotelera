/**
 * A LINK IN THE ANSWER, and the ones to ITS application handled differently (12.51.0).
 *
 * The employee may have a WAI application associated and return links to it («your trip
 * is here»). That link opens ANOTHER origin, with its own session, so whoever had just
 * been talking to it —identified— ran into a login screen: the stumble that gives away
 * that two pieces of the same product do not know each other.
 *
 * IT IS RECOGNISED BY THE HOST, not by an invented mark. The employee writes normal markdown
 * and here it is compared with the address the manifest gives. That way it works the same if
 * the model writes the link on its own —which is what it already did before the association
 * existed—, and it does not depend on it remembering to use a special syntax.
 *
 * THE SESSION IS RESOLVED WITH A PASS valid for sixty seconds and a single use (platform
 * V103). The token does not travel in the URL: what travels is something worthless as soon
 * as it is used. The pass is requested on click, not on paint — requesting it in advance for
 * links nobody is going to touch would mean issuing passes by the handful, and they expire in a minute.
 *
 * And IT IS REQUESTED BY NAVIGATING, NOT WITH `fetch` (12.54.0). The click submits a form to
 * a new tab and the server answers with a 303 to the destination, pass set. Until 12.53 an
 * empty tab was opened on click —opening it after an `await` gets blocked by the browser as
 * a popup— and it was redirected when the pass arrived: the person saw `about:blank` on EVERY
 * link, and handling that tab failed twice. A form is born from the click itself, so there
 * is no popup to block and no tab to handle.
 */
import { useCallback } from 'react'
import { useManifest } from '../../hooks/useManifest'

/** The host of a URL, whether full or bare. Comparing hosts is the only thing both parts
 *  —the employee and this screen— know the same way. */
export function host(x: string): string {
  const d = (x || '').trim().toLowerCase()
  if (!d) return ''
  try {
    return new URL(d.includes('//') ? d : `https://${d}`).hostname.replace(/\.$/, '')
  } catch {
    return ''
  }
}

/** Only these leave the chat as links: a web page, an email or a phone. Anything else
 *  (`exports/…`, `/workspace/…`, `sandbox:`, `file:`, a bare name) is a path inside the
 *  employee's workspace, which has no address in the browser. */
export function esEnlaceWeb(href: string): boolean {
  return /^(https?:\/\/|mailto:|tel:)/i.test((href || '').trim())
}

export function esDeSuAplicacion(href: string, urlDeLaApp?: string | null): boolean {
  if (!href || !urlDeLaApp) return false
  const h = host(href)
  return !!h && h === host(urlDeLaApp)
}

/** Opens the associated application with the session already resolved: a form to
 *  `/api/auth/ir-a-la-aplicacion`, which answers with a 303 to it. If the pass fails, the
 *  server redirects anyway —without a pass—: the person will see the login screen, which is
 *  what always happened until 12.51, much better than a link that does nothing. */
export function abrirSuAplicacion(href: string) {
  const f = document.createElement('form')
  f.method = 'POST'
  f.action = '/api/auth/ir-a-la-aplicacion'
  f.target = '_blank'
  // The application has no business being able to touch this tab (`window.opener`).
  f.setAttribute('rel', 'noopener noreferrer')
  f.style.display = 'none'
  // The token goes in the BODY of a POST to our own origin: neither in the URL nor in the
  // history. It is the same thing that travels in the header of any API call.
  const campos: Record<string, string> = { destino: href, token: localStorage.getItem('token') || '' }
  for (const [nombre, valor] of Object.entries(campos)) {
    const i = document.createElement('input')
    i.type = 'hidden'
    i.name = nombre
    i.value = valor
    f.appendChild(i)
  }
  document.body.appendChild(f)
  f.submit()
  f.remove()
}

/**
 * The `a` renderer for the chat markdown. A normal link opens in another tab
 * —navigating away replaces the conversation, and the turn may be in progress—; one to
 * its application also opens with the session set.
 */
export default function EnlaceDelChat(props: React.AnchorHTMLAttributes<HTMLAnchorElement>) {
  // The address comes from the MANIFEST and not from a prop: that way the two places that
  // paint markdown (the bubble and the turn) stay one line, and the data does not have to be
  // passed through three components that have nothing to do with this. The hook caches in the module.
  const { manifest } = useManifest()
  const href = String(props.href || '')
  const mia = esDeSuAplicacion(href, manifest?.aplicacion?.url)
  const alPulsar = useCallback((ev: React.MouseEvent<HTMLAnchorElement>) => {
    if (!mia) return
    // The browser's «open in another tab» gesture is respected (ctrl/cmd/wheel): whoever
    // uses it knows what they are doing, and there is no click of ours to intercept.
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return
    ev.preventDefault()
    abrirSuAplicacion(href)
  }, [mia, href])

  // A link that is not a web address is not a link (15.10.0). The model, against its
  // instructions («attach the real file, never a link»), sometimes writes «[Download the
  // Excel](exports/x.xlsx)» or `sandbox:/…`: painted as a link it opened the SPA's fallback
  // page (200, no error) and the person thought the file did not exist. The file is in the
  // files panel; the name stays as plain text so it can still be read.
  if (!esEnlaceWeb(href)) return <span>{props.children}</span>

  // The `href` stays CLEAN on purpose: copying the link must not carry a pass inside.
  // The pass is added on opening, and by then we know it is a real click.
  return <a {...props} target="_blank" rel="noopener noreferrer" onClick={alPulsar} />
}
