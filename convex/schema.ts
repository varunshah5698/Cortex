import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

/**
 * Cortex's durable state.
 *
 * The Python harness owns execution: it runs the pipeline, writes the workspace and
 * decides `done`. It is deliberately not in here. What lives in Convex is the state that
 * has to survive a restart and be shared between people and machines — the accounts, the
 * threads, the runs and their events, the specialist roster and the lessons a failed run
 * taught. Before this, threads were in localStorage and accounts were in a SQLite file on
 * an ephemeral disk, so a restart lost the login and a new browser lost the history.
 */
export default defineSchema({
  // One row per person. `token` is the session bearer the browser holds; it is stored
  // hashed so a database read cannot impersonate anyone.
  users: defineTable({
    email: v.string(),
    name: v.optional(v.string()),
    passwordHash: v.string(),
    passwordSalt: v.string(),
    createdAt: v.number(),
    lastSeenAt: v.optional(v.number()),
  }).index("by_email", ["email"]),

  sessions: defineTable({
    userId: v.id("users"),
    tokenHash: v.string(),
    createdAt: v.number(),
    expiresAt: v.number(),
    userAgent: v.optional(v.string()),
  })
    .index("by_token", ["tokenHash"])
    .index("by_user", ["userId"]),

  // A conversation. Messages are their own table so a long thread does not rewrite one
  // growing document on every token.
  threads: defineTable({
    userId: v.id("users"),
    title: v.string(),
    updatedAt: v.number(),
    createdAt: v.number(),
    archived: v.optional(v.boolean()),
  })
    .index("by_user", ["userId"])
    .index("by_user_updated", ["userId", "updatedAt"]),

  messages: defineTable({
    threadId: v.id("threads"),
    role: v.union(v.literal("user"), v.literal("assistant"), v.literal("system")),
    text: v.string(),
    kind: v.optional(v.string()),
    agent: v.optional(v.string()),
    createdAt: v.number(),
  }).index("by_thread", ["threadId"]),

  // A build run. `runId` is the harness's own id, so the two systems agree on identity.
  runs: defineTable({
    userId: v.id("users"),
    runId: v.string(),
    prompt: v.string(),
    depth: v.string(),
    status: v.union(
      v.literal("running"),
      v.literal("done"),
      v.literal("blocked"),
      v.literal("failed"),
      v.literal("cancelled"),
    ),
    files: v.optional(v.array(v.object({ path: v.string(), lines: v.optional(v.number()) }))),
    errors: v.optional(v.number()),
    warnings: v.optional(v.number()),
    costUsd: v.optional(v.number()),
    tokens: v.optional(v.number()),
    message: v.optional(v.string()),
    createdAt: v.number(),
    finishedAt: v.optional(v.number()),
  })
    .index("by_user", ["userId"])
    .index("by_user_created", ["userId", "createdAt"])
    .index("by_run", ["runId"]),

  // The event stream, appended as the harness emits it. This is what lets a run be
  // replayed or watched from another device.
  events: defineTable({
    runId: v.string(),
    seq: v.number(),
    type: v.string(),
    payload: v.any(),
    at: v.number(),
  }).index("by_run_seq", ["runId", "seq"]),

  // The specialist roster: provider, model, role. Never a key — keys stay server-side.
  specialists: defineTable({
    userId: v.id("users"),
    specId: v.string(),
    name: v.string(),
    role: v.string(),
    kind: v.string(),
    baseUrl: v.optional(v.string()),
    model: v.string(),
    ready: v.boolean(),
    updatedAt: v.number(),
  }).index("by_user", ["userId"]),

  // A verification failure that became a lesson, injected into later prompts.
  lessons: defineTable({
    userId: v.optional(v.id("users")),
    text: v.string(),
    code: v.optional(v.string()),
    file: v.optional(v.string()),
    runId: v.optional(v.string()),
    createdAt: v.number(),
  }).index("by_created", ["createdAt"]),
});
