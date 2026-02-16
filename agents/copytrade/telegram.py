import os

import httpx


class TelegramNotifier:
    """Sends trade notifications to Telegram. Optional — disabled if not configured."""

    def __init__(self) -> None:
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.bot_token and self.chat_id)

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            httpx.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
        except Exception:
            pass  # Never let a notification failure crash the bot

    def notify_trade(
        self,
        action: str,
        side: str,
        amount: float,
        price: float,
        market: str,
        balance: float,
        error: str = "",
    ) -> None:
        if action == "DRY_RUN_TRADE":
            emoji = "🧪"
            label = "DRY RUN"
        elif action == "TRADE_EXECUTED":
            emoji = "✅"
            label = "TRADE EXECUTED"
        elif action == "TRADE_REJECTED":
            emoji = "🚫"
            label = "REJECTED"
        elif action == "TRADE_FAILED":
            emoji = "❌"
            label = "FAILED"
        else:
            emoji = "📋"
            label = action

        msg = (
            f"{emoji} <b>{label}</b>\n"
            f"  {side} ${amount:.2f} @ {price:.4f}\n"
            f"  Market: {market[:60]}\n"
            f"  Balance: ${balance:.2f} USDC"
        )
        if error:
            msg += f"\n  Error: {error}"

        self.send(msg)

    def notify_bot_event(self, event: str, details: str) -> None:
        if event == "BOT_START":
            self.send(f"🟢 <b>Bot Started</b>\n{details}")
        elif event == "BOT_STOP":
            self.send(f"🔴 <b>Bot Stopped</b>\n{details}")
        elif event == "BOT_SHUTDOWN":
            self.send(f"🔴 <b>Bot Shutdown (too many errors)</b>\n{details}")
        elif event == "BOT_ERROR":
            self.send(f"⚠️ <b>Error</b>\n{details}")
        elif event == "TRADE_DETECTED":
            self.send(f"👁 <b>Trade Detected</b>\n{details}")
        else:
            self.send(f"📋 <b>{event}</b>\n{details}")
