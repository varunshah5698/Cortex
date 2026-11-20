import { v } from "convex/values";
import { mutation, query } from "./_generated/server";
import type { MutationCtx, QueryCtx } from "./_generated/server";

/**
 * Accounts and sessions.
 *
 * Passwords are hashed with PBKDF2-SHA256 through WebCrypto, which the Convex runtime
 * provides. The session token is random and stored hashed: a leaked database read cannot
 * be replayed as a login, which is the same reason the harness keyring is 0600.
 *
 * The salt is per user and stored beside the hash. This is deliberately not a hand-rolled
 * scheme — it is the standard construction, with an iteration count high enough to be
 * slow on purpose.
 */

const ITERATIONS = 210_000;
const SESSION_MS = 1000 * 60 * 60 * 24 * 30; // 30 days

function toHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function randomHex(bytes: number): string {
  const data = new Uint8Array(bytes);
  crypto.getRandomValues(data);
  return Array.from(data)
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function hash(password: string, salt: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, [
    "deriveBits",
  ]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: new TextEncoder().encode(salt), iterations: ITERATIONS, hash: "SHA-256" },
    key,
    256,
  );
  return toHex(bits);
}

async function tokenHash(token: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  return toHex(digest);
}

/** The signed-in user for a token, or null. Every function that touches user data calls this. */
export async function userFor(ctx: QueryCtx | MutationCtx, token: string | undefined) {
  if (!token) return null;
  const hashed = await tokenHash(token);
  const session = await ctx.db
    .query("sessions")
    .withIndex("by_token", (q) => q.eq("tokenHash", hashed))
    .first();
  if (!session || session.expiresAt < Date.now()) return null;
  return await ctx.db.get(session.userId);
}

export const signUp = mutation({
  args: { email: v.string(), password: v.string(), name: v.optional(v.string()) },
  handler: async (ctx, args) => {
    const email = args.email.trim().toLowerCase();
    if (!email.includes("@")) throw new Error("That does not look like an email address.");
    if (args.password.length < 8) throw new Error("Use at least 8 characters.");

    const existing = await ctx.db
      .query("users")
      .withIndex("by_email", (q) => q.eq("email", email))
      .first();
    if (existing) throw new Error("An account with that email already exists.");

    const salt = randomHex(16);
    const userId = await ctx.db.insert("users", {
      email,
      name: args.name?.trim() || undefined,
      passwordHash: await hash(args.password, salt),
      passwordSalt: salt,
      createdAt: Date.now(),
    });

    const token = randomHex(32);
    await ctx.db.insert("sessions", {
      userId,
      tokenHash: await tokenHash(token),
      createdAt: Date.now(),
      expiresAt: Date.now() + SESSION_MS,
    });
    return { token, email, name: args.name?.trim() || null };
  },
});

export const signIn = mutation({
  args: { email: v.string(), password: v.string() },
  handler: async (ctx, args) => {
    const email = args.email.trim().toLowerCase();
    const user = await ctx.db
      .query("users")
      .withIndex("by_email", (q) => q.eq("email", email))
      .first();
    // Same message either way: a different one tells an attacker which emails exist.
    const rejected = new Error("Email or password is incorrect.");
    if (!user) throw rejected;

    const attempt = await hash(args.password, user.passwordSalt);
    if (attempt !== user.passwordHash) throw rejected;

    const token = randomHex(32);
    await ctx.db.insert("sessions", {
      userId: user._id,
      tokenHash: await tokenHash(token),
      createdAt: Date.now(),
      expiresAt: Date.now() + SESSION_MS,
    });
    await ctx.db.patch(user._id, { lastSeenAt: Date.now() });
    return { token, email: user.email, name: user.name ?? null };
  },
});

export const me = query({
  args: { token: v.optional(v.string()) },
  handler: async (ctx, args) => {
    const user = await userFor(ctx, args.token);
    if (!user) return null;
    return { email: user.email, name: user.name ?? null, createdAt: user.createdAt };
  },
});

export const signOut = mutation({
  args: { token: v.string() },
  handler: async (ctx, args) => {
    const hashed = await tokenHash(args.token);
    const session = await ctx.db
      .query("sessions")
      .withIndex("by_token", (q) => q.eq("tokenHash", hashed))
      .first();
    if (session) await ctx.db.delete(session._id);
    return { ok: true };
  },
});
