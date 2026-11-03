# Morpho Radar automatic alerts — setup

This is a staging build. The existing Telegram bot remains running until you configure and deploy the upgrade.

1. Keep your existing Render `morpho-telegram-bot` Free Web Service, and your BotFather token. Do not create another bot or Render service.
2. Create a **Free** Redis database at https://console.upstash.com/redis . From its REST API panel, copy the `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` values privately into Render > your bot service > Environment.
3. Add another Render environment variable named `MONITOR_SECRET`. Use a new randomly generated 32+ character value. Do **not** share it, paste it into GitHub, or reuse your Telegram token.
4. Once the feature branch is merged or the updated code deployed, wait for Render to say Live. Test `/start`, `/demoalert`, and `/status` in the Telegram bot. Send `/watches` (should be empty); this does not need crypto.
5. In cron-job.org edit your existing `Morpho Radar Keep Alive` job. Change the URL from `/health` to `https://morpho-telegram-bot.onrender.com/monitor`. Set a custom HTTP header with name `X-Monitor-Secret` and value equal to `MONITOR_SECRET` from Render. Keep GET and Every 10 minutes; don't put any secret in the URL. Test Run: should return HTTP **202** with `{"accepted":true,"status":"scheduled"}` (a second test within 7 minutes may say `accepted:false` because the scan lock is active). Do not expose or save secrets in cron execution logs.
6. Test `/status` in Telegram. It should show monitoring configured and a recent completed scan after a scheduled execution. `/demoalert` sends clearly labeled simulated data to the current chat. It is **not proof** a real loan crossed a threshold.
7. For real loans, `/watch <market_id> <wallet_address> [health_factor_threshold]`; then `/watches` and `/unwatch <watch_id>`. A real, active Base Morpho Blue borrowing position is required for registration, not a funded wallet owned by the business.

## Important limitations

- These are **estimated, best-effort** warnings on a ten-minute poll, not guaranteed real-time liquidation protection.
- Morpho REST data can lag on-chain events and is not an atomic snapshot. Users should verify any risk onchain before acting.
- Render Free can suspend/restart and has **750 shared free instance-hours per workspace per month**; DOCSCRUB is another consumer of these hours. The cron schedule does not guarantee uptime.
- The pilot caps each user chat at **3 watches** and the whole deployment at **30 watches** to avoid free-usage exhaustion. It is not a paid subscription or reliable commercial SLA.
- Do not publish paid alert promises until you have validated the stored watches, live `/check` on a public borrowing position, delivery timing, deduplication, and outage behavior.
- This package/branch does not modify the existing paid x402 Morpho Radar Worker.