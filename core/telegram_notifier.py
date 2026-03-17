import requests
import logging
from config import config

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self):
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)
        self._update_fail_count = 0

        if not self.enabled:
            logger.warning("Telegram Bot Token or Chat ID is missing. Notifications are disabled.")

    def send_message(self, text, parse_mode='HTML', edit_last=False, reply_markup=None):
        if not self.enabled:
            return None

        if edit_last and hasattr(self, 'last_msg_id') and self.last_msg_id:
            url = f"https://api.telegram.org/bot{self.token}/editMessageText"
            payload = {"chat_id": self.chat_id, "message_id": self.last_msg_id,
                       "text": text, "parse_mode": parse_mode}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            try:
                response = requests.post(url, json=payload, timeout=10)
                if response.status_code == 200:
                    return self.last_msg_id
            except Exception:
                pass

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            msg_id = response.json().get("result", {}).get("message_id")
            self.last_msg_id = msg_id
            return msg_id
        except Exception as e:
            logger.error(f"Failed to send telegram message: {e}")
            return None

    def delete_message(self, message_id):
        if not self.enabled or not message_id:
            return False
        url = f"https://api.telegram.org/bot{self.token}/deleteMessage"
        try:
            requests.post(url, json={"chat_id": self.chat_id, "message_id": message_id}, timeout=5)
            return True
        except Exception:
            return False

    def answer_callback_query(self, callback_query_id):
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/answerCallbackQuery"
        try:
            requests.post(url, json={"callback_query_id": callback_query_id}, timeout=5)
        except Exception:
            pass

    def get_updates(self, offset=None):
        if not self.enabled:
            return []
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {"timeout": 10, "offset": offset}
        try:
            response = requests.get(url, params=params, timeout=15)
            if response.status_code == 200:
                self._update_fail_count = 0
                return response.json().get("result", [])
        except Exception as e:
            self._update_fail_count += 1
            if self._update_fail_count == 1 or self._update_fail_count % 30 == 0:
                logger.warning(f"Failed to fetch telegram updates ({self._update_fail_count}회): {e}")
        return []


notifier = TelegramNotifier()
