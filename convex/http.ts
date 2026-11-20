import { httpRouter } from "convex/server";
import { httpAction } from "./_generated/server";
import { api } from "./_generated/api";

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

    const seq = await ctx.runMutation(api.runs.appendEventBySecret, {
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

    await ctx.runMutation(api.runs.finishBySecret, {
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

    const id = await ctx.runMutation(api.lessons.recordBySecret, {
      secret: process.env.CONVEX_HARNESS_SECRET ?? "",
      text: body.text,
      code: body.code,
      file: body.file,
      runId: body.runId,
    });
    return json({ ok: true, id });
  }),
});

export default router;
