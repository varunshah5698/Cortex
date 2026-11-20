import { v } from "convex/values";
import { mutation, query } from "./_generated/server";
import { userFor } from "./auth";

/**
 * Threads and messages — the conversation history that used to live in localStorage.
 *
 * Every function scopes to the signed-in user. A thread id is not a capability: passing
 * someone else's id returns nothing rather than their conversation, because the ownership
 * check happens before the read, not after.
 */

export const list = query({
  args: { token: v.optional(v.string()) },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) return [];
    const rows = await ctx.db
      .query("threads")
      .withIndex("by_user_updated", (q) => q.eq("userId", user._id))
      .order("desc")
      .take(100);
    return rows.filter((row) => !row.archived);
  },
});

export const messages = query({
  args: { token: v.optional(v.string()), threadId: v.id("threads") },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) return [];
    const thread = await ctx.db.get(args.threadId);
    if (!thread || thread.userId !== user._id) return [];
    return await ctx.db
      .query("messages")
      .withIndex("by_thread", (q) => q.eq("threadId", args.threadId))
      .take(500);
  },
});

export const create = mutation({
  args: { token: v.string(), title: v.string() },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) throw new Error("Not signed in.");
    const now = Date.now();
    return await ctx.db.insert("threads", {
      userId: user._id,
      title: args.title.slice(0, 200),
      createdAt: now,
      updatedAt: now,
    });
  },
});

export const append = mutation({
  args: {
    token: v.string(),
    threadId: v.id("threads"),
    role: v.union(v.literal("user"), v.literal("assistant"), v.literal("system")),
    text: v.string(),
    kind: v.optional(v.string()),
    agent: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) throw new Error("Not signed in.");
    const thread = await ctx.db.get(args.threadId);
    if (!thread || thread.userId !== user._id) throw new Error("No such thread.");

    const id = await ctx.db.insert("messages", {
      threadId: args.threadId,
      role: args.role,
      text: args.text,
      kind: args.kind,
      agent: args.agent,
      createdAt: Date.now(),
    });
    await ctx.db.patch(args.threadId, { updatedAt: Date.now() });
    return id;
  },
});

export const rename = mutation({
  args: { token: v.string(), threadId: v.id("threads"), title: v.string() },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) throw new Error("Not signed in.");
    const thread = await ctx.db.get(args.threadId);
    if (!thread || thread.userId !== user._id) throw new Error("No such thread.");
    await ctx.db.patch(args.threadId, { title: args.title.slice(0, 200), updatedAt: Date.now() });
    return { ok: true };
  },
});

export const remove = mutation({
  args: { token: v.string(), threadId: v.id("threads") },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) throw new Error("Not signed in.");
    const thread = await ctx.db.get(args.threadId);
    if (!thread || thread.userId !== user._id) throw new Error("No such thread.");

    // Messages go with the thread. A deleted conversation should not leave readable
    // fragments behind for the next query to find.
    const rows = await ctx.db
      .query("messages")
      .withIndex("by_thread", (q) => q.eq("threadId", args.threadId))
      .take(1000);
    for (const row of rows) await ctx.db.delete(row._id);
    await ctx.db.delete(args.threadId);
    return { ok: true, deleted: rows.length };
  },
});
