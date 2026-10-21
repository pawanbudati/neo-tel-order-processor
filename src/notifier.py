import os
import json
import logging
import threading
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger("kotak_trader")

class TelegramNotifier:
    """
    Lightweight, non-blocking Telegram notification dispatcher for consumer trade events.
    Uses daemon threads so execution speed and trading latency are never delayed by network I/O.
    """

    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.enabled = bool(self.bot_token and self.chat_id)

    def _send_sync(self, text: str):
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                pass
        except Exception as e:
            logger.warning(f"Telegram notification delivery failed: {e}")

    def send(self, text: str):
        """Asynchronously dispatches a notification in the background."""
        if not self.enabled:
            return
        t = threading.Thread(target=self._send_sync, args=(text,), daemon=True)
        t.start()

    def notify_order_placed(
        self,
        order_id: str,
        symbol: str,
        action: str,
        quantity: int,
        price: float,
        trigger_price: float,
        stop_loss: Optional[float] = None,
        targets: Optional[list] = None
    ):
        sl_str = f" | SL: `{stop_loss}`" if stop_loss else ""
        t_str = f" | Targets: `{targets}`" if targets else ""
        text = (
            f"🛒 *Kotak Order Placed*\n\n"
            f"• *Symbol*: `{symbol}`\n"
            f"• *Action*: `{action.upper()}` | *Qty*: `{quantity}`\n"
            f"• *Price*: `{price}` (Trigger: `{trigger_price}`)\n"
            f"• *Order ID*: `{order_id}`{sl_str}{t_str}"
        )
        self.send(text)

    def notify_order_failed(self, symbol: str, error: str):
        text = (
            f"⚠️ *Kotak Order Placement Failed*\n\n"
            f"• *Symbol*: `{symbol}`\n"
            f"• *Error*: `{error}`"
        )
        self.send(text)

    def notify_exit_update(self, symbol: str, message: str):
        text = f"🎯 *Position Update* (`{symbol}`)\n\n{message}"
        self.send(text)

    def notify_startup(self, account_id: str):
        text = f"🚀 *Kotak Order Processor Started*\nAccount: `{account_id}`\nWaiting for trading signals from Kafka..."
        self.send(text)


notifier = TelegramNotifier()
