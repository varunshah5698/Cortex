/**
 * One API client for the browser: every `/api` request carries the session cookie, and
 * a 401 lands back on the sign-in screen instead of leaving a page silently empty.
 */
let installed = false;

export function installApiClient() {
  if (installed) return;
  installed = true;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === "string" ? input : input instanceof Request ? input.url : String(input);
    if (!url.includes("/api/")) return nativeFetch(input, init);

    const response = await nativeFetch(input, { credentials: "include", ...init });
    // A failed stop is not a sign-out. Stop/close calls are advisory; keeping the room
    // mounted matters more than treating one 401 as an expired session.
    const quiet = /\/api\/run\/[^/]+\/(stop|serve\/stop|share\/stop)$/.test(url.split("?")[0]);
    if (response.status === 401 && !quiet && !url.includes("/api/auth/")) {
      window.dispatchEvent(new Event("cortex:unauthorized"));
    }
    return response;
  };
}
