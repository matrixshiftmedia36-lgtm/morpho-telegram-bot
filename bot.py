import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MORPHO_API_URL = "https://blue-api.morpho.org/graphql"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🛡️ *Morpho Radar Liquidation Guard (Base)*\n\n"
        "Send `/check <market_id> <user_address>` to check your liquidation risk.\n\n"
        "Example:\n`/check 0x123... 0xabc...`"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/check <market_id> <user_address>`", parse_mode="Markdown")
        return

    market_id = context.args[0]
    user_address = context.args[1]
    await update.message.reply_text(f"🔍 Checking liquidation risk on Base for `{user_address[:6]}...`", parse_mode="Markdown")
    
    query = """
    query GetPosition($user: String!, $market: String!) {
      userByAddress(address: $user) {
        positionOnMarket(marketId: $market) {
          healthFactor
        }
      }
    }
    """
    try:
        res = requests.post(MORPHO_API_URL, json={"query": query, "variables": {"user": user_address, "market": market_id}}, timeout=10)
        data = res.json()
        hf = data.get("data", {}).get("userByAddress", {}).get("positionOnMarket", {}).get("healthFactor")
        
        if hf:
            hf_float = float(hf)
            status_emoji = "🟢 Safe" if hf_float > 1.2 else "🔴 CRITICAL RISK"
            msg = f"📊 *Position Status:* {status_emoji}\n*Health Factor:* `{hf_float:.2f}`\nLiquidation threshold: `1.00`\n\n💡 *Want 24/7 automated alerts? Visit:* https://rarefrequencyworks.com"
        else:
            msg = "⚠️ Position not found or no debt active on this market."
    except Exception as e:
        msg = f"Error querying Morpho protocol: {str(e)}"

    await update.message.reply_text(msg, parse_mode="Markdown")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.run_polling()
