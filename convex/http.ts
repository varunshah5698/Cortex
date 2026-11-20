import { httpRouter } from "convex/server";
import { httpAction } from "./_generated/server";
import { api, internal } from "./_generated/api";

/**
 * The HTTP surface, served at the deployment's `.convex.site` origin.
 *
 * This exists for the one caller that is not a browser: the Python harness. It has no
 * Convex client and no session cookie, so it posts run events here with a shared secret
 * (`CONVEX_HARNESS_SECRET`) instead. Everything a browser does goes through the typed
 * functions in `threads.ts` / `runs.ts`, not through these routes.
 *
 * The secret is compared before anything is read or written, and a missing secret is a
 * refusal rather than an open door — an unset variable must not mean "no auth required".
 */

const router = httpRouter();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "access-control-allow-origin": "*" },
  });
}

function authorized(request: Request): boolean {
  const expected = process.env.CONVEX_HARNESS_SECRET;
  if (!expected) return false;
  const header = request.headers.get("authorization") ?? "";
  const presented = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (presented.length !== expected.length) return false;
  // Constant-time-ish: compare every byte so a timing signal does not leak the prefix.
  let diff = 0;
  for (let i = 0; i < expected.length; i += 1) diff |= presented.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

router.route({
  path: "/health",
  method: "GET",
  handler: httpAction(async () => json({ ok: true, service: "cortex-convex" })),
});

/** The harness posts one event at a time; it already has the run id and the payload. */
router.route({
  path: "/harness/event",
  method: "POST",
  handler: httpAction(async (ctx, request) => {
    if (!authorized(request)) return json({ error: "unauthorized" }, 401);
    let body: { runId?: string; type?: string; payload?: unknown };
    try {
      body = await request.json();
    } catch {
      return json({ error: "invalid json" }, 400);
    }
    if (!body.runId || !body.type) return json({ error: "runId and type are required" }, 400);

    const seq = await ctx.runMutation(internal.runs.appendEventBySecret, {
      secret: process.env.CONVEX_HARNESS_SECRET ?? "",
      runId: body.runId,
      type: body.type,
      payload: body.payload ?? {},
    });
    return json({ ok: true, seq });
  }),
});

/** A run's terminal verdict, so the harness does not need a session to close a run. */
router.route({
  path: "/harness/finish",
  method: "POST",
  handler: httpAction(async (ctx, request) => {
    if (!authorized(request)) return json({ error: "unauthorized" }, 401);
    let body: { runId?: string; status?: string; message?: string; errors?: number };
    try {
      body = await request.json();
    } catch {
      return json({ error: "invalid json" }, 400);
    }
    if (!body.runId || !body.status) return json({ error: "runId and status are required" }, 400);

    await ctx.runMutation(internal.runs.finishBySecret, {
      secret: process.env.CONVEX_HARNESS_SECRET ?? "",
      runId: body.runId,
      status: body.status as "running" | "done" | "blocked" | "failed" | "cancelled",
      message: body.message,
      errors: body.errors,
    });
    return json({ ok: true });
  }),
});

/** A lesson the harness learned, so it survives the machine that learned it. */
router.route({
  path: "/harness/lesson",
  method: "POST",
  handler: httpAction(async (ctx, request) => {
    if (!authorized(request)) return json({ error: "unauthorized" }, 401);
    let body: { text?: string; code?: string; file?: string; runId?: string };
    try {
      body = await request.json();
    } catch {
      return json({ error: "invalid json" }, 400);
    }
    if (!body.text) return json({ error: "text is required" }, 400);

    const id = await ctx.runMutation(internal.lessons.recordBySecret, {
      secret: process.env.CONVEX_HARNESS_SECRET ?? "",
      text: body.text,
      code: body.code,
      file: body.file,
      runId: body.runId,
    });
    return json({ ok: true, id });
  }),
});

// ---------- Google sign-in ----------
// Google redirects the browser here with `?code=...&state=...`. The exchange needs a
// server-side POST carrying the client secret, which is why this is an HTTP action and
// not a Convex function: the secret must never reach the browser, and a browser cannot
// read the response of a cross-origin token exchange anyway.
//
// The order is deliberate — state first, then the code. A callback with a valid-looking
// code but an unknown state is refused before any request is made to Google, so a forged
// callback costs nothing and cannot be replayed.

const GOOGLE_TOKEN = "https://oauth2.googleapis.com/token";
const GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo";

/**
 * Who does this session token belong to?
 *
 * The Python harness calls this to turn a Convex session into its own `cortex_session`
 * cookie. It is the only route here that is not guarded by the harness secret, because
 * its whole purpose is to answer a caller that has no secret — the browser's token is the
 * credential, and an unknown or expired token gets a 401 rather than an identity.
 *
 * It returns the email and nothing else: no user id, no session, no way to enumerate.
 */
router.route({
  path: "/auth/whoami",
  method: "POST",
  handler: httpAction(async (ctx, request) => {
    let body: { token?: string };
    try {
      body = await request.json();
    } catch {
      return json({ error: "invalid json" }, 400);
    }
    if (!body.token) return json({ error: "token is required" }, 400);

    const account = await ctx.runQuery(api.auth.me, { token: body.token });
    if (!account) return json({ error: "unknown session" }, 401);
    return json({ email: account.email, name: account.name });
  }),
});

/** Where the browser lands after a successful sign-in. */
function appOrigin(): string {
  // The app is served by Render; the Convex site is a different origin, so the redirect
  // has to be absolute. `APP_ORIGIN` is the deployment's own URL and is set per
  // environment rather than guessed from a request header, which a caller controls.
  return (process.env.APP_ORIGIN ?? "").replace(/\/$/, "");
}

/**
 * Send the browser back to the login page with a message it can actually read.
 *
 * The app is a hash router, so everything after `#` is the route and the query string
 * before it belongs to the document. A parameter set with `searchParams` lands *before*
 * the `#`, where the router never looks — the person sees a plain login page and no
 * reason for it. The parameters therefore go inside the fragment, next to the route.
 */
function bounce(target: string, params: Record<string, string>): Response {
  const url = new URL(target);
  const hash = url.hash || "#/login";
  const at = hash.indexOf("?");
  const route = at === -1 ? hash : hash.slice(0, at);
  const existing = new URLSearchParams(at === -1 ? "" : hash.slice(at + 1));
  for (const [key, value] of Object.entries(params)) existing.set(key, value);
  url.hash = `${route}?${existing.toString()}`;
  return new Response(null, { status: 302, headers: { location: url.toString() } });
}

router.route({
  path: "/api/auth/callback/google",
  method: "GET",
  handler: httpAction(async (ctx, request) => {
    const url = new URL(request.url);
    const code = url.searchParams.get("code");
    const state = url.searchParams.get("state");
    const denied = url.searchParams.get("error");
    const origin = appOrigin();
    const back = origin ? `${origin}/#/login` : "/#/login";

    if (denied) return bounce(back, { error: "Google sign-in was cancelled." });
    if (!code || !state) return bounce(back, { error: "Google returned no authorization code." });

    // Single-use, and consumed before the exchange so a replayed callback cannot retry.
    const pending = await ctx.runMutation(api.auth.consumeState, { state });
    if (!pending) return bounce(back, { error: "That sign-in link expired. Try again." });

    const clientId = process.env.GOOGLE_CLIENT_ID;
    const clientSecret = process.env.GOOGLE_CLIENT_SECRET;
    const site = process.env.CONVEX_SITE_URL;
    if (!clientId || !clientSecret || !site) {
      return bounce(back, { error: "Google sign-in is not configured on this deployment." });
    }

    let profile: { sub?: string; email?: string; name?: string; picture?: string };
    try {
      const tokenResponse = await fetch(GOOGLE_TOKEN, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          code,
          client_id: clientId,
          client_secret: clientSecret,
          redirect_uri: `${site}/api/auth/callback/google`,
          grant_type: "authorization_code",
        }),
      });
      if (!tokenResponse.ok) {
        // The body can name the misconfiguration (a wrong redirect_uri, an expired code).
        // It is Google's text, not a secret, and it is the difference between a five
        // minute fix and an afternoon.
        const detail = await tokenResponse.text();
        console.error("google token exchange failed", tokenResponse.status, detail.slice(0, 400));
        return bounce(back, { error: "Google rejected the sign-in. Check the OAuth client settings." });
      }
      const tokens = (await tokenResponse.json()) as { access_token?: string };
      if (!tokens.access_token) return bounce(back, { error: "Google returned no access token." });

      const userResponse = await fetch(GOOGLE_USERINFO, {
        headers: { authorization: `Bearer ${tokens.access_token}` },
      });
      if (!userResponse.ok) return bounce(back, { error: "Could not read your Google profile." });
      profile = (await userResponse.json()) as typeof profile;
    } catch (error) {
      console.error("google sign-in failed", error);
      return bounce(back, { error: "Could not reach Google. Try again." });
    }

    if (!profile.sub || !profile.email) {
      return bounce(back, { error: "Google did not share an email address." });
    }

    const session = await ctx.runMutation(api.auth.signInWithGoogle, {
      googleId: profile.sub,
      email: profile.email,
      name: profile.name,
      picture: profile.picture,
    });

    // The token rides in the fragment, not the query string: a fragment is never sent to
    // a server, so it stays out of access logs and Referer headers. The login page reads
    // it once and clears it from the address bar.
    // The token rides inside the fragment, after the route, for the same reason the error
    // does: the router only reads what follows `#`. A fragment is never sent to a server,
    // so it stays out of access logs and Referer headers, and the login page clears it
    // from the address bar as soon as it has been adopted.
    return bounce(origin ? `${origin}/#/login` : "/#/login", {
      token: session.token,
      email: session.email,
    });
  }),
});

export default router;
