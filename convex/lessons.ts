import { v } from "convex/values";
import { internalMutation, query } from "./_generated/server";
import { userFor } from "./auth";

/**
 * Lessons: a verification failure that became a durable instruction.
 *
 * The harness already writes these to `.harness-lessons.md` and injects them into the
 * planner and the code prompt. Storing them here as well is what lets a lesson outlive one
 * machine — the whole point of a lesson is that it applies to the next run, not the next
 * process.
 */

export const recent = query({
  args: { token: v.optional(v.string()), limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    // Lessons carry file paths and failure detail from real runs, so this is not public.
    // A signed-out caller gets nothing rather than everyone's failures.
    const user = await userFor(ctx, args.token);
    if (!user) return [];
    const rows = await ctx.db
      .query("lessons")
      .withIndex("by_created")
      .order("desc")
      .take(Math.min(args.limit ?? 20, 100));
    return rows.filter((row) => !row.userId || row.userId === user._id);
  },
});

export const record = internalMutation({
  args: {
    text: v.string(),
    code: v.optional(v.string()),
    file: v.optional(v.string()),
    runId: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const text = args.text.trim();
    if (!text) return null;
    // One row per distinct lesson: a failure that repeats is not new knowledge.
    const existing = await ctx.db.query("lessons").withIndex("by_created").order("desc").take(200);
    if (existing.some((row) => row.text === text)) return null;
    return await ctx.db.insert("lessons", {
      text: text.slice(0, 1000),
      code: args.code,
      file: args.file,
      runId: args.runId,
      createdAt: Date.now(),
    });
  },
});

/** The harness's write path; see the note in `runs.ts` on why the secret is re-checked. */
export const recordBySecret = internalMutation({
  args: {
    secret: v.string(),
    text: v.string(),
    code: v.optional(v.string()),
    file: v.optional(v.string()),
    runId: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const expected = process.env.CONVEX_HARNESS_SECRET;
    if (!expected || args.secret !== expected) throw new Error("unauthorized");
    const text = args.text.trim();
    if (!text) return null;
    const existing = await ctx.db.query("lessons").withIndex("by_created").order("desc").take(200);
    if (existing.some((row) => row.text === text)) return null;
    return await ctx.db.insert("lessons", {
      text: text.slice(0, 1000),
      code: args.code,
      file: args.file,
      runId: args.runId,
      createdAt: Date.now(),
    });
  },
});
