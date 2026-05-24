from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class AIReplyService:
    def __init__(self):
        self.api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
        self.base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "25"))

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model)

    def build_messages(self, profile: dict, history: list[dict]) -> list[dict]:
        style_prompt = (profile.get("style_prompt") or "").strip()
        communication_style = (profile.get("communication_style") or "Деловой").strip()
        chat_profile = profile.get("chat_profile") or {}
        style_examples = profile.get("style_examples") or []
        system = (
            "Ты помощник Fin Usluga. Отвечай на русском языке."
            " Не упоминай, что ты ИИ или бот."
            " Не придумывай факты о платежах, курсах, статусах и сроках."
            " Если данных не хватает, задай один короткий уточняющий вопрос."
        )
        if communication_style == "Дружеский":
            system += " Тон дружелюбный, теплый, вежливый, но все еще аккуратный и короткий."
        else:
            system += " Тон официальный, спокойный, вежливый, короткий."
        if style_prompt:
            system += f" Дополнительный стиль: {style_prompt}"
        if chat_profile:
            common_currencies = ", ".join((chat_profile.get("common_currencies") or {}).keys())
            common_keywords = ", ".join(list((chat_profile.get("common_keywords") or {}).keys())[:10])
            hints = []
            if common_currencies:
                hints.append(f"Частые валюты клиента: {common_currencies}.")
            if common_keywords:
                hints.append(f"Частые слова клиента: {common_keywords}.")
            if chat_profile.get("last_exchange_direction"):
                hints.append(f"Последнее направление обмена: {chat_profile['last_exchange_direction']}.")
            if hints:
                system += " Контекст клиента: " + " ".join(hints)
        messages = [{"role": "system", "content": system}]
        if style_examples:
            examples_lines = []
            for example in style_examples[-6:]:
                client_text = (example.get("client_text") or "").strip()
                manual_reply = (example.get("manual_reply") or "").strip()
                if client_text and manual_reply:
                    examples_lines.append(f"Клиент: {client_text}\nТы обычно отвечаешь: {manual_reply}")
            if examples_lines:
                messages.append({
                    "role": "system",
                    "content": (
                        "Ниже примеры того, как обычно отвечает владелец именно этому клиенту."
                        " Подражай тону, длине и формулировкам, но не копируй дословно без необходимости.\n\n"
                        + "\n\n".join(examples_lines)
                    ),
                })
        messages.extend(history)
        return messages

    def generate_reply(self, profile: dict, history: list[dict]) -> str | None:
        if not self.enabled:
            return None
        payload = {
            "model": self.model,
            "messages": self.build_messages(profile, history),
            "temperature": 0.4,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            return None
        choices = data.get("choices") or []
        if not choices:
            return None
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip() or None
        if isinstance(content, list):
            parts = [item.get("text", "") for item in content if isinstance(item, dict)]
            text = "".join(parts).strip()
            return text or None
        return None

    def generate_exchange_reply(
        self,
        profile: dict,
        history: list[dict],
        instruction: str,
    ) -> str | None:
        if not self.enabled:
            return None
        payload = {
            "model": self.model,
            "messages": self.build_messages(
                profile,
                history
                + [
                    {
                        "role": "system",
                        "content": (
                            "Сейчас ты помогаешь оформить заявку на обмен."
                            " Отвечай очень коротко, естественно и по-человечески."
                            " Не используй списки, markdown, кавычки вокруг примеров и канцелярит."
                            " Если нужно уточнение, задай один короткий вопрос."
                        ),
                    },
                    {"role": "user", "content": instruction},
                ],
            ),
            "temperature": 0.5,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            return None
        choices = data.get("choices") or []
        if not choices:
            return None
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip() or None
        if isinstance(content, list):
            parts = [item.get("text", "") for item in content if isinstance(item, dict)]
            text = "".join(parts).strip()
            return text or None
        return None
