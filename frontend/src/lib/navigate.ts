/**
 * Leave the single-page app with a full page load. Used after signing in or out: nothing of the
 * previous session's page may stay in memory, and the address can be the identity provider's.
 */
export function hardNavigate(url: string): void {
  window.location.assign(url);
}
