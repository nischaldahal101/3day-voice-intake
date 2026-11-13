// Cloudflare Email Worker for voiceconsult.app (stage E5).
// Email Routing catch-all -> this Worker -> POST /inbound/cart on our orchestrator.
// All logic lives in handoff.js (testable with `node --test`).
import { handleEmail } from "./handoff.js";

export default {
  async email(message, env, ctx) {
    await handleEmail(message, env);
  },
};
