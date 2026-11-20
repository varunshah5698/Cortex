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
    // A Google-only account has no password to compare against. Refusing here keeps the
    // message identical to a wrong password, so it does not reveal that the account exists.
    const { passwordHash, passwordSalt } = user;
    if (!passwordHash || !passwordSalt) throw rejected;

    const attempt = await hash(args.password, passwordSalt);
    if (attempt !== passwordHash) throw rejected;

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

// ---------- Google sign-in ----------
//
// The OAuth exchange happens in `convex/http.ts`, because it needs `fetch` and the
// redirect back from Google, neither of which a Convex function can do. What lives here
// is the part that must be transactional and must never be reachable from a browser: the
// state nonce, and the account creation that turns a verified Google profile into a
// session.
//
// The client secret is read from the deployment environment, never from the repo and
// never from an argument: a value passed in by a caller is a value an attacker can set.

const STATE_MS = 1000 * 60 * 10; // 10 minutes to finish signing in

/** Mint a single-use state nonce and return the URL Google should send the browser to. */
export const startGoogle = mutation({
  args: { redirectTo: v.optional(v.string()) },
  handler: async (ctx, args) => {
    const clientId = process.env.GOOGLE_CLIENT_ID;
    const site = process.env.CONVEX_SITE_URL;
    if (!clientId || !site) {
      throw new Error("Google sign-in is not configured on this deployment.");
    }

    const state = randomHex(24);
    await ctx.db.insert("oauthStates", {
      state,
      redirectTo: args.redirectTo ?? "/",
      createdAt: Date.now(),
      expiresAt: Date.now() + STATE_MS,
    });

    const url = new URL("https://accounts.google.com/o/oauth2/v2/auth");
    url.searchParams.set("client_id", clientId);
    url.searchParams.set("redirect_uri", `${site}/api/auth/callback/google`);
    url.searchParams.set("response_type", "code");
    url.searchParams.set("scope", "openid email profile");
    url.searchParams.set("state", state);
    // `select_account` so a shared machine does not silently reuse the last account.
    url.searchParams.set("prompt", "select_account");
    return { url: url.toString(), state };
  },
});

/**
 * Consume a state nonce. Returns the redirect target, or null if it is unknown, used or
 * expired. Deleting on read is what makes it single-use: a replayed callback finds nothing.
 */
export const consumeState = mutation({
  args: { state: v.string() },
  handler: async (ctx, args) => {
    const row = await ctx.db
      .query("oauthStates")
      .withIndex("by_state", (q) => q.eq("state", args.state))
      .first();
    if (!row) return null;
    await ctx.db.delete(row._id);
    if (row.expiresAt < Date.now()) return null;
    return { redirectTo: row.redirectTo };
  },
});

/**
 * Turn a Google profile that Google has already vouched for into a session.
 *
 * The caller (`convex/http.ts`) has exchanged the code for a token and read the profile
 * from Google's userinfo endpoint, so `googleId` and `email` here are Google's claims, not
 * the browser's. Matching order matters: the stable subject id first, then the email, so
 * someone who already had a password account and later uses Google lands in the same
 * account instead of a duplicate.
 */
export const signInWithGoogle = mutation({
  args: {
    googleId: v.string(),
    email: v.string(),
    name: v.optional(v.string()),
    picture: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const email = args.email.trim().toLowerCase();
    if (!args.googleId) throw new Error("Google did not return a subject id.");
    if (!email.includes("@")) throw new Error("Google did not return an email address.");

    let user = await ctx.db
      .query("users")
      .withIndex("by_google", (q) => q.eq("googleId", args.googleId))
      .first();

    if (!user) {
      const byEmail = await ctx.db
        .query("users")
        .withIndex("by_email", (q) => q.eq("email", email))
        .first();
      if (byEmail) {
        // Link rather than duplicate: same person, second way in.
        await ctx.db.patch(byEmail._id, {
          googleId: args.googleId,
          picture: args.picture,
          lastSeenAt: Date.now(),
        });
        user = await ctx.db.get(byEmail._id);
      } else {
        const userId = await ctx.db.insert("users", {
          email,
          name: args.name?.trim() || undefined,
          googleId: args.googleId,
          picture: args.picture,
          createdAt: Date.now(),
          lastSeenAt: Date.now(),
        });
        user = await ctx.db.get(userId);
      }
    } else {
      await ctx.db.patch(user._id, { lastSeenAt: Date.now() });
    }

    if (!user) throw new Error("Could not create the account.");

    const token = randomHex(32);
    await ctx.db.insert("sessions", {
      userId: user._id,
      tokenHash: await tokenHash(token),
      createdAt: Date.now(),
      expiresAt: Date.now() + SESSION_MS,
    });
    return { token, email: user.email, name: user.name ?? null };
  },
});
