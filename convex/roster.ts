import { v } from "convex/values";
import { mutation, query } from "./_generated/server";
import { userFor } from "./auth";

/**
 * The specialist roster: which provider and model each role uses.
 *
 * Deliberately no `apiKey` field. Provider keys live in the harness keyring on the server
 * (`modules/roster.py`, `HARNESS_KEYS_FILE`, mode 0600) and are never read back to a
 * browser. A schema that cannot hold a key is a stronger guarantee than a rule saying not
 * to store one.
 */

export const list = query({
  args: { token: v.optional(v.string()) },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) return [];
    return await ctx.db
      .query("specialists")
      .withIndex("by_user", (q) => q.eq("userId", user._id))
      .take(50);
  },
});

export const save = mutation({
  args: {
    token: v.string(),
    specId: v.string(),
    name: v.string(),
    role: v.string(),
    kind: v.string(),
    baseUrl: v.optional(v.string()),
    model: v.string(),
    ready: v.boolean(),
  },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) throw new Error("Not signed in.");

    const rows = await ctx.db
      .query("specialists")
      .withIndex("by_user", (q) => q.eq("userId", user._id))
      .take(50);
    const existing = rows.find((row) => row.specId === args.specId);
    const body = {
      userId: user._id,
      specId: args.specId,
      name: args.name,
      role: args.role,
      kind: args.kind,
      baseUrl: args.baseUrl,
      model: args.model,
      ready: args.ready,
      updatedAt: Date.now(),
    };
    if (existing) {
      await ctx.db.patch(existing._id, body);
      return existing._id;
    }
    return await ctx.db.insert("specialists", body);
  },
});

export const remove = mutation({
  args: { token: v.string(), specId: v.string() },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) throw new Error("Not signed in.");
    const rows = await ctx.db
      .query("specialists")
      .withIndex("by_user", (q) => q.eq("userId", user._id))
      .take(50);
    const existing = rows.find((row) => row.specId === args.specId);
    if (existing) await ctx.db.delete(existing._id);
    return { ok: true };
  },
});
