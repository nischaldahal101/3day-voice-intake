# Cart email Worker (stage E5)

A tiny Cloudflare Email Worker. Every email sent to `anything@voiceconsult.app` goes to this Worker,
which hands it to our backend (`POST https://api.voiceconsult.app/inbound/cart`). The backend decides
whether it's a real cart from a real user (address exists, sender is that user's verified email or
Ferguson, SPF/DKIM/DMARC pass). The Worker itself decides nothing about content.

**What it does with each email**

| Backend answer | Worker does |
|---|---|
| 2xx (stored, duplicate, or "dropped") | Nothing more. Silent. |
| 400 / 401 / 403 / 411 / 413 (any other 4xx) | Drops it silently and logs the status number. |
| 5xx, 429, network error or timeout (30 s) | Waits 2 s and tries **once** more. Still failing → refuses the email with the generic message "Temporary problem, please forward the cart again later." |
| Email bigger than 5 MB | Drops it silently (`dropped too_big`). |
| `INBOUND_EMAIL_SECRET` not set on the Worker | Drops it silently and logs `config_missing_secret` (a setup mistake must not bounce every email). |

**It never bounces rejected mail.** Unknown addresses get the same quiet treatment as everything else,
so nobody can probe which addresses exist. The temporary message only appears when our backend is down,
and it says nothing about the address.

Logs only ever contain fixed codes and status numbers, never addresses, subjects, content or the secret.

Code: `src/handoff.js` (all logic), `src/index.js` (Worker entry). Tests: `node --test test/*.test.mjs`
(no Cloudflare account needed).

---

## Setup (one time)

You need: the Cloudflare account that owns `voiceconsult.app`, the Render dashboard, and Node installed.

### 1. Make a secret

In a terminal:

```
openssl rand -hex 32
```

Copy the long value it prints. You'll paste the **same** value in two places (steps 2 and 4).
Don't commit it or paste it into chat.

### 2. Put the secret on Render (orchestrator only)

Render → the **orchestrator** service → Environment:
- Add `INBOUND_EMAIL_SECRET` = the value from step 1.
- Check `INBOUND_EMAIL_DOMAIN` is either not set or exactly `voiceconsult.app`.
- Save (Render redeploys).

Until this is set, the backend answers 503 and accepts nothing.

### 3. Turn on Email Routing

Cloudflare dashboard → `voiceconsult.app` → **Email** → **Email Routing** → **Get started / Enable**.
Cloudflare will offer to add its MX and SPF DNS records. Accept them.

> **Warning:** if `voiceconsult.app` already receives email somewhere else (Google Workspace, Fastmail, ...),
> enabling Email Routing replaces those MX records and that mailbox stops receiving mail. Stop and ask
> first if that's the case.

### 4. Deploy the Worker

```
cd mockfieldpulse/email-worker
npx wrangler login
npx wrangler deploy
npx wrangler secret put INBOUND_EMAIL_SECRET
```

Paste the **same** value as step 1 when `secret put` asks for it.

### 5. Send all mail for the domain to the Worker

Cloudflare → `voiceconsult.app` → Email → Email Routing → **Routing rules** → **Catch-all address** →
Edit → Action **Send to a Worker** → `voiceconsult-cart-email` → Save, and make sure the catch-all is **enabled**.

---

## First real test (do this once)

1. Turn on the one-time debug log: in `wrangler.toml` remove the `#` in front of
   `DEBUG_AUTH_RESULTS = "1"` and run `npx wrangler deploy` again.
2. In a second terminal: `npx wrangler tail` (leave it running).
3. Open the app → **Carts**. Your address is shown at the top (e.g. `nick@voiceconsult.app`).
4. From **your own email address that you signed in to the app with** (it must be verified in Clerk),
   forward a Ferguson cart email to that address.
5. Watch the tail. You should see something like:
   - `cart-email: debug Authentication-Results: mx.cloudflare.net; spf=pass ...`
   - `cart-email: handed_off status=202 stored`
6. The cart should appear in the app under **Carts** within a few seconds.
7. Send the `debug Authentication-Results: ...` line to Claude so we can confirm the backend's
   SPF/DKIM/DMARC check matches what Cloudflare really writes. It contains your own sender address and
   domain, and no customer data. If the tail says `debug no Authentication-Results header; header names: ...`,
   send that line instead (header names only).
8. Turn the debug log **off**: put the `#` back in front of `DEBUG_AUTH_RESULTS` in `wrangler.toml`, and
   `npx wrangler deploy` again.

---

## Troubleshooting

Start with `npx wrangler tail` while you forward a cart, and look at the `cart-email:` line.

| Tail shows | Meaning | Fix |
|---|---|---|
| nothing at all | The email never reached the Worker. | Step 3 / step 5: Email Routing enabled? Catch-all enabled and set to the Worker? |
| `config_missing_secret` | Worker has no secret. | Step 4: `npx wrangler secret put INBOUND_EMAIL_SECRET`. |
| `backend_unavailable status=503` (then "rejected as temporary") with no reason | The secret isn't set on Render, or the backend is down. | Step 2. Check the orchestrator is running. |
| `... status=503 user_lookup_failed` | Couldn't read your Clerk profile. | Temporary; forward again later. If it repeats, check `CLERK_SECRET_KEY` on Render. |
| `... status=503 storage_unavailable` | Couldn't save to R2. | Temporary; check R2 settings on Render. |
| `backend_refused status=401` | The two secrets differ. | Repeat step 4 with exactly the Render value (or set both again). |
| `backend_refused status=413` / `dropped too_big` | Email bigger than 5 MB. | Forward the cart without attachments. |
| `backend_refused status=400` or `411` | The request was malformed (a bug). | Tell Claude. |
| `status=0 timeout` / `status=0 network_error` | Couldn't reach the backend. | Check `api.voiceconsult.app` is up; check `INBOUND_URL` in `wrangler.toml`. |
| `handed_off status=202 duplicate` | You already forwarded this exact email. | Nothing; the existing cart is kept. |
| `handed_off status=202 <reason>` | The backend dropped it on purpose. See below. | |

**Drop reasons** (also in the Render orchestrator logs as `Inbound cart email dropped: <reason>`):

| Reason | Plain meaning |
|---|---|
| `bad_address` | The to/from address couldn't be read. |
| `wrong_domain` | Sent to a domain other than `INBOUND_EMAIL_DOMAIN` (check that Render setting). |
| `unknown_alias` | No user has that address. Open the app → Carts to see your exact address. |
| `sender_not_allowed` | Not from Ferguson and not forwarded from one of your **verified** sign-in emails. Forward from the email you sign in with, or verify that address in your account. |
| `auth_failed` | Forwarded from one of your sign-in emails but its SPF/DKIM/DMARC check didn't pass, OR the email says it is from `@fergusonhome.com` but Cloudflare's DMARC check for fergusonhome.com didn't pass (possible fake). Do the "first real test" debug step and send Claude the line. |

**Two ways a cart is accepted** (verified 2026-11-12 on a real direct Ferguson email):
1. **Straight from Ferguson:** the email's `From:` is an `@fergusonhome.com` address and Cloudflare's (topmost)
   `Authentication-Results` says `dmarc=pass header.from=fergusonhome.com`. Ferguson's envelope sender is a
   `bounces+...@em9668.fergusonhome.com` address; it is ignored for this path.
2. **Forwarded by you:** the envelope sender is one of your verified sign-in emails and passes SPF/DKIM/DMARC.

**CPU limit:** on the Workers Free plan each email gets about 10 ms of CPU. A normal cart email is small and
fine; a very large email (several MB) may hit the limit and fail. If the tail shows "exceeded CPU" errors,
the Workers Paid plan removes this.
