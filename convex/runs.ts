import { v } from "convex/values";
import { mutation, query } from "./_generated/server";
import { userFor } from "./auth";

/**
 * Build runs and their event stream.
 *
 * The harness executes; this records. `runId` is the harness's own id so a run has one
 * identity across both systems, and `events` is append-only with a monotonic `seq` so a
 * client can resume a stream from where it stopped instead of replaying the whole run.
 */

const STATUS = v.union(
  v.literal("running"),
  v.literal("done"),
  v.literal("blocked"),
  v.literal("failed"),
  v.literal("cancelled"),
);

export const start = mutation({
  args: { token: v.string(), runId: v.string(), prompt: v.string(), depth: v.string() },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) throw new Error("Not signed in.");

    const existing = await ctx.db
      .query("runs")
      .withIndex("by_run", (q) => q.eq("runId", args.runId))
      .first();
    if (existing) return existing._id;

    return await ctx.db.insert("runs", {
      userId: user._id,
      runId: args.runId,
      prompt: args.prompt.slice(0, 4000),
      depth: args.depth,
      status: "running",
      createdAt: Date.now(),
    });
  },
});

export const appendEvent = mutation({
  args: { token: v.string(), runId: v.string(), type: v.string(), payload: v.any() },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) throw new Error("Not signed in.");
    const run = await ctx.db
      .query("runs")
      .withIndex("by_run", (q) => q.eq("runId", args.runId))
      .first();
    if (!run || run.userId !== user._id) throw new Error("No such run.");

    const last = await ctx.db
      .query("events")
      .withIndex("by_run_seq", (q) => q.eq("runId", args.runId))
      .order("desc")
      .first();
    const seq = (last?.seq ?? -1) + 1;
    await ctx.db.insert("events", { runId: args.runId, seq, type: args.type, payload: args.payload, at: Date.now() });
    return seq;
  },
});

export const finish = mutation({
  args: {
    token: v.string(),
    runId: v.string(),
    status: STATUS,
    files: v.optional(v.array(v.object({ path: v.string(), lines: v.optional(v.number()) }))),
    errors: v.optional(v.number()),
    warnings: v.optional(v.number()),
    costUsd: v.optional(v.number()),
    tokens: v.optional(v.number()),
    message: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) throw new Error("Not signed in.");
    const run = await ctx.db
      .query("runs")
      .withIndex("by_run", (q) => q.eq("runId", args.runId))
      .first();
    if (!run || run.userId !== user._id) throw new Error("No such run.");

    await ctx.db.patch(run._id, {
      status: args.status,
      files: args.files,
      errors: args.errors,
      warnings: args.warnings,
      costUsd: args.costUsd,
      tokens: args.tokens,
      message: args.message?.slice(0, 2000),
      finishedAt: Date.now(),
    });
    return { ok: true };
  },
});

export const history = query({
  args: { token: v.optional(v.string()), limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) return [];
    return await ctx.db
      .query("runs")
      .withIndex("by_user_created", (q) => q.eq("userId", user._id))
      .order("desc")
      .take(Math.min(args.limit ?? 50, 200));
  },
});

export const events = query({
  args: { token: v.optional(v.string()), runId: v.string(), after: v.optional(v.number()) },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) return [];
    const run = await ctx.db
      .query("runs")
      .withIndex("by_run", (q) => q.eq("runId", args.runId))
      .first();
    if (!run || run.userId !== user._id) return [];

    const rows = await ctx.db
      .query("events")
      .withIndex("by_run_seq", (q) => q.eq("runId", args.runId))
      .order("asc")
      .take(2000);
    const after = args.after ?? -1;
    return rows.filter((row) => row.seq > after);
  },
});

/**
 * The harness's own write path. It has no session, so it presents the shared secret that
 * `http.ts` already verified; this re-checks it because an internal function is callable
 * from anywhere in the deployment, and a check that lives only in the caller is not a check.
 */
function requireSecret(presented: string): void {
  const expected = process.env.CONVEX_HARNESS_SECRET;
  if (!expected || presented !== expected) throw new Error("unauthorized");
}

export const appendEventBySecret = mutation({
  args: { secret: v.string(), runId: v.string(), type: v.string(), payload: v.any() },
  handler: async (ctx, args) => {
    requireSecret(args.secret);
    const last = await ctx.db
      .query("events")
      .withIndex("by_run_seq", (q) => q.eq("runId", args.runId))
      .order("desc")
      .first();
    const seq = (last?.seq ?? -1) + 1;
    await ctx.db.insert("events", {
      runId: args.runId,
      seq,
      type: args.type,
      payload: args.payload,
      at: Date.now(),
    });
    return seq;
  },
});

export const finishBySecret = mutation({
  args: {
    secret: v.string(),
    runId: v.string(),
    status: STATUS,
    message: v.optional(v.string()),
    errors: v.optional(v.number()),
  },
  handler: async (ctx, args) => {
    requireSecret(args.secret);
    const run = await ctx.db
      .query("runs")
      .withIndex("by_run", (q) => q.eq("runId", args.runId))
      .first();
    if (!run) return { ok: false, reason: "no such run" };
    await ctx.db.patch(run._id, {
      status: args.status,
      message: args.message?.slice(0, 2000),
      errors: args.errors,
      finishedAt: Date.now(),
    });
    return { ok: true };
  },
});
