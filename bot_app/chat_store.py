from __future__ import annotations

import json
import mimetypes
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update

from .utils import normalize_chat_text


class ChatStore:
    def __init__(self, root: str = "data/chats"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _chat_dir(self, chat_id: str) -> Path:
        path = self.root / chat_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _attachments_dir(self, chat_id: str) -> Path:
        path = self._chat_dir(chat_id) / "attachments"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _history_path(self, chat_id: str) -> Path:
        return self._chat_dir(chat_id) / "history.jsonl"

    def _profile_path(self, chat_id: str) -> Path:
        return self._chat_dir(chat_id) / "profile.json"

    def _chat_profile_path(self, chat_id: str) -> Path:
        return self._chat_dir(chat_id) / "chat_profile.json"

    def _business_connections_path(self) -> Path:
        path = self.root / "business_connections.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _requests_path(self, chat_id: str) -> Path:
        return self._chat_dir(chat_id) / "requests.jsonl"

    def _exchange_patterns_path(self, chat_id: str) -> Path:
        return self._chat_dir(chat_id) / "exchange_patterns.jsonl"

    def _style_examples_path(self, chat_id: str) -> Path:
        return self._chat_dir(chat_id) / "style_examples.jsonl"

    def get_all_chat_ids(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(path.name for path in self.root.iterdir() if path.is_dir())

    def ensure_profile(self, chat_id: str, chat_name: str, user_name: str) -> None:
        data = self.load_profile(chat_id)
        data["chat_id"] = chat_id
        data["chat_name"] = chat_name
        data["user_name"] = user_name
        data.setdefault("style_prompt", "")
        data.setdefault("greeting_counters", {})
        self._profile_path(chat_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_profile(self, chat_id: str) -> dict:
        path = self._profile_path(chat_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def ensure_chat_profile(self, chat_id: str, chat_name: str, user_name: str) -> None:
        data = self.load_chat_profile(chat_id)
        data["chat_id"] = chat_id
        data["chat_name"] = chat_name
        data["user_name"] = user_name
        data.setdefault("client_message_count", 0)
        data.setdefault("manual_reply_count", 0)
        data.setdefault("exchange_request_count", 0)
        data.setdefault("common_currencies", {})
        data.setdefault("common_keywords", {})
        data.setdefault("last_client_text", "")
        data.setdefault("last_manual_reply", "")
        data.setdefault("last_exchange_direction", "")
        self._chat_profile_path(chat_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_chat_profile(self, chat_id: str) -> dict:
        path = self._chat_profile_path(chat_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_chat_profile(self, chat_id: str, data: dict) -> None:
        self._chat_profile_path(chat_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_active_exchange(self, chat_id: str) -> dict | None:
        data = self.load_profile(chat_id)
        return data.get("active_exchange")

    def set_active_exchange(self, chat_id: str, payload: dict | None) -> None:
        data = self.load_profile(chat_id)
        data["chat_id"] = chat_id
        if payload is None:
            data.pop("active_exchange", None)
        else:
            data["active_exchange"] = payload
        self._profile_path(chat_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_next_greeting_index(self, chat_id: str, style_key: str, variants_count: int) -> int:
        data = self.load_profile(chat_id)
        counters = data.setdefault("greeting_counters", {})
        current = int(counters.get(style_key, 0))
        next_index = current % max(variants_count, 1)
        counters[style_key] = current + 1
        self._profile_path(chat_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return next_index

    def log_system_event(self, chat_id: str, event: str, payload: dict | None = None) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "direction": "system",
            "role": "system",
            "chat_id": chat_id,
            "event": event,
        }
        if payload:
            entry["payload"] = payload
        self.append_entry(chat_id, entry)

    def append_entry(self, chat_id: str, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._history_path(chat_id).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def log_incoming_text_entry(
        self,
        chat_id: str,
        chat_name: str,
        user_id: int | None,
        user_name: str,
        text: str,
        *,
        message_id: int | None = None,
        source: str = "telegram",
    ) -> None:
        if not text:
            return
        self.ensure_chat_profile(chat_id, chat_name, user_name)
        self.append_entry(chat_id, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "direction": "incoming",
            "role": "user",
            "message_id": message_id,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "user_id": user_id,
            "user_name": user_name,
            "text": text,
            "source": source,
        })
        self.update_chat_profile_from_client_text(chat_id, chat_name, user_name, text)

    def log_incoming_text(self, update: Update) -> None:
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not message or not chat or not user:
            return
        chat_id = str(chat.id)
        chat_name = chat.title or chat.username or user.full_name or chat_id
        user_name = user.full_name or user.username or str(user.id)
        self.ensure_profile(chat_id, chat_name, user_name)
        text = message.text or message.caption or ""
        self.log_incoming_text_entry(chat_id, chat_name, user.id, user_name, text, message_id=message.message_id)

    def log_outgoing_text(self, chat_id: str, chat_name: str, text: str, message_id: int | None = None) -> None:
        self.append_entry(chat_id, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "direction": "outgoing",
            "role": "assistant",
            "message_id": message_id,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "text": text,
        })

    def update_chat_profile_from_client_text(self, chat_id: str, chat_name: str, user_name: str, text: str) -> None:
        data = self.load_chat_profile(chat_id)
        self.ensure_chat_profile(chat_id, chat_name, user_name)
        data = self.load_chat_profile(chat_id)
        data["client_message_count"] = int(data.get("client_message_count", 0)) + 1
        data["last_client_text"] = text
        words = [word for word in normalize_chat_text(text).split() if len(word) >= 3]
        keyword_counter = Counter(data.get("common_keywords", {}))
        keyword_counter.update(words[:12])
        data["common_keywords"] = dict(keyword_counter.most_common(40))
        self._save_chat_profile(chat_id, data)

    def update_chat_profile_from_exchange(self, chat_id: str, give_currency: str, receive_currency: str) -> None:
        data = self.load_chat_profile(chat_id)
        if not data:
            return
        data["exchange_request_count"] = int(data.get("exchange_request_count", 0)) + 1
        data["last_exchange_direction"] = f"{give_currency}->{receive_currency}"
        currency_counter = Counter(data.get("common_currencies", {}))
        currency_counter.update([give_currency, receive_currency])
        data["common_currencies"] = dict(currency_counter.most_common(12))
        self._save_chat_profile(chat_id, data)

    def append_style_example(
        self,
        chat_id: str,
        *,
        chat_name: str,
        client_text: str,
        manual_reply: str,
        user_name: str,
        message_id: int | None = None,
    ) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "chat_id": chat_id,
            "chat_name": chat_name,
            "user_name": user_name,
            "message_id": message_id,
            "client_text": client_text,
            "manual_reply": manual_reply,
        }
        with self._style_examples_path(chat_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        data = self.load_chat_profile(chat_id)
        if data:
            data["manual_reply_count"] = int(data.get("manual_reply_count", 0)) + 1
            data["last_manual_reply"] = manual_reply
            self._save_chat_profile(chat_id, data)

    def get_recent_style_examples(self, chat_id: str, limit: int = 20) -> list[dict]:
        path = self._style_examples_path(chat_id)
        if not path.exists():
            return []
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows[-limit:]

    def get_similar_style_examples(self, chat_id: str, current_text: str, limit: int = 6) -> list[dict]:
        rows = self.get_recent_style_examples(chat_id, limit=200)
        current_tokens = set(normalize_chat_text(current_text).split())
        if not current_tokens:
            return rows[-limit:]
        scored: list[tuple[float, dict]] = []
        for row in rows:
            client_text = (row.get("client_text") or "").strip()
            if not client_text:
                continue
            example_tokens = set(normalize_chat_text(client_text).split())
            if not example_tokens:
                continue
            overlap = len(current_tokens & example_tokens)
            union = len(current_tokens | example_tokens)
            score = overlap / union if union else 0.0
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in scored[:limit]] or rows[-limit:]

    def count_style_examples(self, chat_id: str) -> int:
        path = self._style_examples_path(chat_id)
        if not path.exists():
            return 0
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())

    def count_dialog_history_entries(self, chat_id: str) -> int:
        path = self._history_path(chat_id)
        if not path.exists():
            return 0
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("role") in {"user", "assistant"} and (entry.get("text") or "").strip():
                count += 1
        return count

    def save_attachment_copy(
        self,
        chat_id: str,
        source_path: str,
        *,
        original_name: str,
        media_kind: str,
        message_id: int | None = None,
        caption: str = "",
        user_id: int | None = None,
        user_name: str = "",
    ) -> str:
        src = Path(source_path)
        suffix = src.suffix or Path(original_name).suffix
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_name = f"{ts}_{media_kind}_{src.stem}{suffix}"
        dst = self._attachments_dir(chat_id) / safe_name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        mime_type, _ = mimetypes.guess_type(str(dst))
        self.append_entry(chat_id, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "direction": "incoming",
            "role": "user",
            "message_id": message_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "user_name": user_name,
            "media_kind": media_kind,
            "file_name": original_name,
            "stored_path": str(dst),
            "mime_type": mime_type or "",
            "caption": caption,
        })
        return str(dst)

    def get_recent_dialogue(self, chat_id: str, limit: int = 20) -> list[dict]:
        path = self._history_path(chat_id)
        if not path.exists():
            return []
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("role") not in {"user", "assistant"}:
                continue
            text = (entry.get("text") or "").strip()
            if not text:
                continue
            rows.append({"role": entry["role"], "text": text})
        return rows[-limit:]

    def _load_business_connections(self) -> dict:
        path = self._business_connections_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def set_business_connection(self, connection_id: str, user_id: int, user_chat_id: int) -> None:
        data = self._load_business_connections()
        data[connection_id] = {"user_id": user_id, "user_chat_id": user_chat_id}
        self._business_connections_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_business_connection(self, connection_id: str) -> dict:
        return self._load_business_connections().get(connection_id, {})

    def append_exchange_request(self, chat_id: str, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._requests_path(chat_id).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def append_exchange_pattern(self, chat_id: str, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._exchange_patterns_path(chat_id).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def get_exchange_patterns(self, chat_id: str, limit: int = 200) -> list[dict]:
        path = self._exchange_patterns_path(chat_id)
        if not path.exists():
            return []
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows[-limit:]
