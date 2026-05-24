from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    BusinessConnectionHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .ai_service import AIReplyService
from .audio_transcriber import AudioTranscriber
from .chat_store import ChatStore
from .constants import ALIAS_TO_CURRENCY, OCR_ALIAS_TO_CODE, SUPPORTED_CURRENCY_ALIASES, SUPPORTED_CURRENCY_ORDER
from .models import ExchangeRequestRow, PendingReceiptEntry, SheetRow
from .receipt_service import ReceiptProcessor
from .services import ExpressionEvaluator
from .sheet_repo import SheetRepository
from .utils import format_decimal, looks_like_expression, looks_like_greeting, normalize_chat_text, round_exchange_amount

LOGGER = logging.getLogger("sheet_math_bot")


class FinuslugaBot:
    def __init__(self):
        self.token = os.environ["TELEGRAM_TOKEN"]
        self.evaluator = ExpressionEvaluator()
        self.repo = SheetRepository(set(SUPPORTED_CURRENCY_ALIASES.keys()))
        self.ai_service = AIReplyService()
        self.audio_transcriber = AudioTranscriber()
        self.receipt_processor = ReceiptProcessor()
        self.receipts_dir = Path(os.environ.get("RECEIPTS_DIR", "receipts"))
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.chat_store = ChatStore(os.environ.get("CHAT_DATA_DIR", "data/chats"))
        self.pending_receipts: dict[str, PendingReceiptEntry] = {}
        self.manual_receipt_inputs: dict[int, str] = {}
        self._status_sync_task: asyncio.Task | None = None

    def _is_known_command_text(self, text: str) -> bool:
        token = text.strip().split(maxsplit=1)[0].casefold()
        return token in {
            "/start",
            "/menu",
            "/summa",
            "/сумма",
            "/total",
            "/kurs",
            "/курс",
        }

    def _menu_markup(self, rows: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
        keyboard_rows = list(rows or [])
        keyboard_rows.append([InlineKeyboardButton("Меню", callback_data="menu|main|open")])
        return InlineKeyboardMarkup(keyboard_rows)

    def _menu_text(self) -> str:
        return (
            "Пиши боту так: `@FinUslugaTR_bot 100+25 usd`\n"
            "Умеет: показать баланс, показать курс, распознать чек."
        )

    def _menu_rows(self) -> list[list[InlineKeyboardButton]]:
        return [[
            InlineKeyboardButton("Баланс", callback_data="menu|main|summa"),
            InlineKeyboardButton("Курс", callback_data="menu|main|kurs"),
        ]]

    def _looks_like_exchange_intent(self, text: str) -> bool:
        lowered = text.casefold()
        keywords = (
            "обмен",
            "обменять",
            "менять",
            "обмен нужен",
            "курс",
            "купить",
            "продать",
            "получить",
            "отдать",
            "закинуть",
            "скинуть",
            "перевести",
            "оплатить",
            "можешь",
            "нужен доллар",
            "нужны доллары",
            "нужен перевод",
        )
        if any(keyword in lowered for keyword in keywords):
            return True
        items = self._extract_exchange_items(text)
        amount_only = self._extract_amount_only(text)
        if len(items) >= 2:
            return True
        if len(items) == 1:
            remainder = lowered.replace(items[0]["raw"].lower(), " ")
            if self._extract_target_currency(remainder):
                return True
        if amount_only is not None and self._extract_target_currency(text):
            if any(token in lowered for token in ("нужно", "надо", "хочу", "получ", "купить", "еще", "ещё")):
                return True
        currencies = re.findall(r"[A-Za-zА-Яа-я€$₺₸₽]+", text)
        seen = {
            (
                OCR_ALIAS_TO_CODE.get(token)
                if token in OCR_ALIAS_TO_CODE
                else ALIAS_TO_CURRENCY.get(token.lower())
            )
            for token in currencies
        }
        seen.discard(None)
        return len(seen) >= 2

    def _extract_exchange_items(self, text: str) -> list[dict]:
        items: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for match in re.finditer(r"(\d[\d\s.,]*[кkKК]?)\s*([A-Za-zА-Яа-я]+)", text):
            amount = self._parse_manual_value(match.group(1))
            token = match.group(2).lower()
            currency = ALIAS_TO_CURRENCY.get(token)
            if amount is None or not currency:
                continue
            key = (format_decimal(amount.copy_abs()), currency)
            if key in seen:
                continue
            seen.add(key)
            display_currency = "USDC" if token in {"usdc", "юсдц"} else currency
            items.append({
                "amount": format_decimal(amount.copy_abs()),
                "currency": currency,
                "display_currency": display_currency,
                "raw": f"{format_decimal(amount.copy_abs())} {display_currency}",
            })
        for match in re.finditer(r"(\d[\d\s.,]*[кkKК]?)\s*([€$₺₸₽])", text):
            amount = self._parse_manual_value(match.group(1))
            currency = OCR_ALIAS_TO_CODE.get(match.group(2))
            if amount is None or not currency:
                continue
            key = (format_decimal(amount.copy_abs()), currency)
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "amount": format_decimal(amount.copy_abs()),
                "currency": currency,
                "raw": f"{format_decimal(amount.copy_abs())} {currency}",
            })
        for match in re.finditer(r"([€$₺₸₽])\s*(\d[\d\s.,]*[кkKК]?)", text):
            currency = OCR_ALIAS_TO_CODE.get(match.group(1))
            amount = self._parse_manual_value(match.group(2))
            if amount is None or not currency:
                continue
            key = (format_decimal(amount.copy_abs()), currency)
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "amount": format_decimal(amount.copy_abs()),
                "currency": currency,
                "raw": f"{format_decimal(amount.copy_abs())} {currency}",
            })
        return items

    def _extract_target_currency(self, text: str) -> str | None:
        for symbol in re.findall(r"[€$₺₸₽]", text):
            currency = OCR_ALIAS_TO_CODE.get(symbol)
            if currency:
                return currency
        for token in re.findall(r"[A-Za-zА-Яа-я]+", text):
            currency = ALIAS_TO_CURRENCY.get(token.lower())
            if currency:
                return currency
        return None

    def _extract_amount_only(self, text: str) -> Decimal | None:
        match = re.search(r"(\d[\d\s.,]*)", text)
        if not match:
            return None
        return self._parse_manual_value(match.group(1))

    def _classify_exchange_side(self, text: str) -> str:
        lowered = text.casefold()
        if re.search(r"\b(можешь|можете|сможете)\b", lowered) and re.search(
            r"\b(закинуть|скинуть|перевести)\b", lowered
        ):
            return "receive"
        if re.search(r"\b(нужно|надо|хочу)\b", lowered) and re.search(
            r"\b(закинуть|скинуть|перевести)\b", lowered
        ):
            return "receive"
        receive_markers = (
            "получ", "нужно", "надо", "хочу", "купить", "нужен", "нужна", "нужны",
            "требуется", "требую", "требуется",
        )
        give_markers = (
            "отда", "отдам", "прода", "меняю", "готов отдать", "есть", "имею",
            "с меня", "скинуть", "перевести", "оплатить", "закинуть", "в кассу", "карта",
        )
        has_receive = any(marker in lowered for marker in receive_markers)
        has_give = any(marker in lowered for marker in give_markers)
        if has_receive and not has_give:
            return "receive"
        if has_give and not has_receive:
            return "give"
        return "give"

    def _split_exchange_clauses(self, text: str) -> list[str]:
        protected = re.sub(r"(?<=\d)[.,](?=\d)", "§", text)
        parts = re.split(r"->|→|[,:;.!?]+|\n+|\s+и\s+|\s+а\s+", protected, flags=re.IGNORECASE)
        parts = [part.replace("§", ".") for part in parts]
        return [part.strip() for part in parts if part.strip()]

    def _seed_exchange_from_text(self, text: str) -> tuple[dict | None, dict | None, str | None]:
        give = None
        receive = None
        seeded_side = None
        clauses = self._split_exchange_clauses(text)
        for clause in clauses:
            side_kind = self._classify_exchange_side(clause)
            items = self._extract_exchange_items(clause)
            if items:
                entry = items[0]
                extra_currency = None
                for token in re.findall(r"[A-Za-zА-Яа-я€$₺₸₽]+", clause):
                    currency = (
                        OCR_ALIAS_TO_CODE.get(token)
                        if token in OCR_ALIAS_TO_CODE
                        else ALIAS_TO_CURRENCY.get(token.lower())
                    )
                    if currency and currency != entry["currency"]:
                        extra_currency = {"currency": currency, "raw": currency}
                        break
                if extra_currency:
                    if side_kind == "receive":
                        if give is None:
                            give = entry
                        if receive is None:
                            receive = extra_currency
                        seeded_side = "both"
                        continue
                    if side_kind == "give":
                        if give is None:
                            give = entry
                        if receive is None:
                            receive = extra_currency
                        seeded_side = "both"
                        continue
                if side_kind == "receive" and receive is None:
                    receive = entry
                    seeded_side = "receive" if seeded_side is None else seeded_side
                elif side_kind == "give" and give is None:
                    give = entry
                    seeded_side = "give" if seeded_side is None else seeded_side
                elif give is None:
                    give = entry
                    seeded_side = "give" if seeded_side is None else seeded_side
                elif receive is None and entry["currency"] != give.get("currency"):
                    receive = entry
                continue
            currency_only = self._extract_currency_only_side(clause)
            if not currency_only:
                continue
            if side_kind == "receive" and receive is None:
                receive = currency_only
                seeded_side = "receive" if seeded_side is None else seeded_side
            elif side_kind == "give" and give is None:
                give = currency_only
                seeded_side = "give" if seeded_side is None else seeded_side
            elif give is None:
                give = currency_only
                seeded_side = "give" if seeded_side is None else seeded_side
            elif receive is None and currency_only["currency"] != give.get("currency"):
                receive = currency_only
        if give and receive:
            return give, receive, "both"
        if give:
            return give, receive, seeded_side or "give"
        if receive:
            return give, receive, seeded_side or "receive"
        return None, None, None

    def _extract_currency_only_side(self, text: str) -> dict | None:
        currency = self._extract_target_currency(text)
        if not currency:
            return None
        lowered = text.casefold()
        display_currency = "USDC" if "usdc" in lowered or "юсдц" in lowered else currency
        return {"currency": currency, "display_currency": display_currency, "raw": display_currency}

    def _is_complete_side(self, side: dict | None) -> bool:
        return bool(side and side.get("currency") and side.get("amount"))

    def _has_currency_only(self, side: dict | None) -> bool:
        return bool(side and side.get("currency") and not side.get("amount"))

    def _format_exchange_side(self, side: dict | None, fallback_prefix: str) -> str:
        if not side:
            return fallback_prefix
        if side.get("raw"):
            return side["raw"]
        if side.get("display_currency"):
            return side["display_currency"]
        if side.get("currency"):
            return side["currency"]
        return fallback_prefix

    def _display_currency_code(self, side: dict | None) -> str:
        if not side:
            return ""
        return side.get("display_currency") or side.get("currency") or ""

    def _build_exchange_pattern_signature(self, text: str) -> str:
        signature = text.casefold()
        for symbol, code in {"€": " eur ", "$": " usd ", "₺": " try ", "₸": " kzt ", "₽": " rub "}.items():
            signature = signature.replace(symbol, code)
        signature = re.sub(r"\b(\d[\d\s.,]*[кkKК]?)\b", " <amount> ", signature)
        tokens = []
        for token in re.findall(r"[a-zA-Zа-яА-ЯёЁ_<>]+", signature):
            lowered = token.casefold()
            if lowered == "amount":
                tokens.append("<amount>")
                continue
            currency = ALIAS_TO_CURRENCY.get(lowered)
            tokens.append((currency or lowered).lower())
        return " ".join(tokens)

    def _extract_amount_side_from_source(self, source_text: str, give_currency: str, receive_currency: str) -> str | None:
        items = self._extract_exchange_items(source_text)
        if len(items) != 1:
            return None
        if items[0]["currency"] == give_currency:
            return "give"
        if items[0]["currency"] == receive_currency:
            return "receive"
        return None

    def _pattern_similarity(self, left_signature: str, right_signature: str) -> float:
        left_tokens = {token for token in left_signature.split() if token != "<amount>"}
        right_tokens = {token for token in right_signature.split() if token != "<amount>"}
        if not left_tokens or not right_tokens:
            return 0.0
        overlap = len(left_tokens & right_tokens)
        union = len(left_tokens | right_tokens)
        return overlap / union if union else 0.0

    def _apply_pattern_memory(self, chat_id: str, raw_text: str) -> tuple[dict | None, dict | None, str | None]:
        signature = self._build_exchange_pattern_signature(raw_text)
        patterns = self.chat_store.get_exchange_patterns(chat_id)
        matched_pattern = None
        for pattern in reversed(patterns):
            if pattern.get("signature") == signature:
                matched_pattern = pattern
                break
        if matched_pattern is None:
            best_score = 0.0
            for pattern in reversed(patterns):
                candidate_signature = pattern.get("signature") or ""
                score = self._pattern_similarity(signature, candidate_signature)
                if score >= 0.72 and score > best_score:
                    best_score = score
                    matched_pattern = pattern
        if matched_pattern is not None:
            pattern = matched_pattern
            give_currency = pattern.get("give_currency")
            receive_currency = pattern.get("receive_currency")
            if not give_currency or not receive_currency:
                return None, None, None
            give = {"currency": give_currency, "display_currency": pattern.get("give_display") or give_currency, "raw": pattern.get("give_display") or give_currency}
            receive = {"currency": receive_currency, "display_currency": pattern.get("receive_display") or receive_currency, "raw": pattern.get("receive_display") or receive_currency}
            for item in self._extract_exchange_items(raw_text):
                if item["currency"] == give_currency:
                    give = item
                elif item["currency"] == receive_currency:
                    receive = item
            amount_only = self._extract_amount_only(raw_text)
            if amount_only is not None and not self._extract_exchange_items(raw_text):
                amount_text = format_decimal(amount_only.copy_abs())
                amount_side = pattern.get("amount_side")
                if amount_side == "give":
                    give["amount"] = amount_text
                    give["raw"] = f"{amount_text} {self._display_currency_code(give)}"
                elif amount_side == "receive":
                    receive["amount"] = amount_text
                    receive["raw"] = f"{amount_text} {self._display_currency_code(receive)}"
            return give, receive, "pattern"
        return None, None, None

    def _remember_exchange_pattern(self, chat_id: str, exchange: dict) -> None:
        source_text = (exchange.get("source_text") or "").strip()
        if not source_text:
            return
        give = exchange.get("give") or {}
        receive = exchange.get("receive") or {}
        if not give.get("currency") or not receive.get("currency"):
            return
        self.chat_store.append_exchange_pattern(chat_id, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source_text": source_text,
            "signature": self._build_exchange_pattern_signature(source_text),
            "give_currency": give["currency"],
            "give_display": self._display_currency_code(give),
            "receive_currency": receive["currency"],
            "receive_display": self._display_currency_code(receive),
            "amount_side": self._extract_amount_side_from_source(source_text, give["currency"], receive["currency"]),
        })

    def _exchange_ready_for_confirmation(self, exchange: dict) -> bool:
        give = exchange.get("give")
        receive = exchange.get("receive")
        return (
            (self._is_complete_side(give) and bool(receive and receive.get("currency")))
            or (self._is_complete_side(receive) and bool(give and give.get("currency")))
        )

    def _build_exchange_summary(self, exchange: dict) -> str:
        give = exchange.get("give")
        receive = exchange.get("receive")
        give_line = self._format_exchange_side(give, "не указано")
        if self._has_currency_only(receive):
            receive_line = receive["currency"]
            return (
                "Подтвердите заявку:\n"
                f"Вы отдаёте: {give_line}\n"
                f"Получить хотите: {receive_line}?\n\n"
                "Проверьте данные заявки."
            )
        receive_line = self._format_exchange_side(receive, "не указано")
        if self._has_currency_only(give):
            return (
                "Подтвердите заявку:\n"
                f"Вы хотите получить: {receive_line}\n"
                f"Отдать хотите: {give_line}?\n\n"
                "Проверьте данные заявки."
            )
        return (
            "Подтвердите заявку:\n"
            f"Отдаёте: {give_line}\n"
            f"Получаете: {receive_line}\n\n"
            "Проверьте данные заявки."
        )

    async def _exchange_reply(
        self,
        chat_id: str,
        current_text: str,
        instruction: str,
        fallback: str,
    ) -> str:
        profile = await self._build_ai_profile(chat_id, current_text)
        history = self.chat_store.get_recent_dialogue(chat_id, limit=12)
        reply = self.ai_service.generate_exchange_reply(profile, history, instruction)
        return reply or fallback

    async def _handle_exchange_flow(
        self,
        chat_id: str,
        raw_text: str,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        business_chat_id: int,
        business_connection_id: str,
        chat_name: str,
    ) -> bool:
        exchange = self.chat_store.get_active_exchange(chat_id)
        seeded_side: str | None = None
        if not exchange and not self._looks_like_exchange_intent(raw_text):
            return False

        if exchange is None:
            give, receive, seeded_side = self._apply_pattern_memory(chat_id, raw_text)
            if give is None and receive is None:
                give, receive, seeded_side = self._seed_exchange_from_text(raw_text)
            if give is None and receive is None:
                items = self._extract_exchange_items(raw_text)
                side_kind = self._classify_exchange_side(raw_text)
                if len(items) >= 2:
                    give = items[0]
                    receive = items[1]
                    seeded_side = "both"
                elif len(items) == 1:
                    if side_kind == "receive":
                        receive = items[0]
                        seeded_side = "receive"
                    else:
                        give = items[0]
                        seeded_side = "give"
                else:
                    currency_only = self._extract_currency_only_side(raw_text)
                    if currency_only:
                        if side_kind == "receive":
                            receive = currency_only
                            seeded_side = "receive"
                        else:
                            give = currency_only
                            seeded_side = "give"
            exchange = {
                "id": uuid4().hex,
                "status": "collecting",
                "give": give,
                "receive": receive,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_text": raw_text,
            }
            self.chat_store.set_active_exchange(chat_id, exchange)

        lowered = raw_text.casefold().strip()
        if exchange.get("status") == "awaiting_edit_choice":
            if "отда" in lowered:
                exchange["status"] = "editing_give"
                exchange["give"] = None
                self.chat_store.set_active_exchange(chat_id, exchange)
                reply = await self._exchange_reply(
                    chat_id,
                    raw_text,
                    "Попроси клиента заново написать, что он отдаёт, коротко и естественно.",
                    "Хорошо. Напишите заново, что вы отдаёте, например 1000 USD.",
                )
                sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
                self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                return True
            if "получ" in lowered:
                exchange["status"] = "editing_receive"
                exchange["receive"] = None
                self.chat_store.set_active_exchange(chat_id, exchange)
                reply = await self._exchange_reply(
                    chat_id,
                    raw_text,
                    "Попроси клиента заново написать, что он хочет получить, коротко и естественно.",
                    "Хорошо. Напишите заново, что вы хотите получить, например 85000 RUB.",
                )
                sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
                self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                return True
            reply = await self._exchange_reply(
                chat_id,
                raw_text,
                "Коротко спроси, что именно нужно исправить в заявке: то, что клиент отдаёт, или то, что получает.",
                "Что нужно исправить: отдаёте или получаете?",
            )
            sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
            self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
            return True

        if exchange.get("status") == "editing_give":
            items = self._extract_exchange_items(raw_text)
            if not items:
                reply = await self._exchange_reply(
                    chat_id,
                    raw_text,
                    "Мягко скажи, что ты не понял, что клиент отдаёт, и попроси написать сумму и валюту одним сообщением.",
                    "Не до конца понял. Напишите, пожалуйста, что вы отдаёте, например 1000 USD.",
                )
                sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
                self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                return True
            exchange["give"] = items[0]
            exchange["status"] = "collecting"
            self.chat_store.set_active_exchange(chat_id, exchange)

        if exchange.get("status") == "editing_receive":
            items = self._extract_exchange_items(raw_text)
            if items:
                exchange["receive"] = items[0]
                exchange["status"] = "collecting"
                self.chat_store.set_active_exchange(chat_id, exchange)
            else:
                target_currency = self._extract_target_currency(raw_text)
                if target_currency:
                    exchange["receive"] = {"currency": target_currency, "raw": target_currency}
                    exchange["status"] = "collecting"
                    self.chat_store.set_active_exchange(chat_id, exchange)
                else:
                    reply = await self._exchange_reply(
                        chat_id,
                        "Мягко скажи, что ты не понял, что клиент хочет получить, и попроси написать сумму и валюту одним сообщением.",
                        "Не до конца понял. Напишите, пожалуйста, что вы хотите получить, например 85000 RUB.",
                    )
                    sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
                    self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                    return True

        if exchange.get("status") == "awaiting_give_amount":
            amount = self._extract_amount_only(raw_text)
            give_currency = (exchange.get("give") or {}).get("currency")
            if amount is None or not give_currency:
                reply = await self._exchange_reply(
                    chat_id,
                    raw_text,
                    "Мягко попроси написать сумму, которую клиент готов отдать.",
                    "Подскажите, пожалуйста, какую сумму вы отдаёте.",
                )
                sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
                self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                return True
            exchange["give"] = {
                "amount": format_decimal(amount.copy_abs()),
                "currency": give_currency,
                "raw": f"{format_decimal(amount.copy_abs())} {give_currency}",
            }
            exchange["status"] = "collecting"
            self.chat_store.set_active_exchange(chat_id, exchange)

        if exchange.get("status") == "awaiting_receive_amount":
            amount = self._extract_amount_only(raw_text)
            receive_currency = (exchange.get("receive") or {}).get("currency")
            if amount is None or not receive_currency:
                reply = await self._exchange_reply(
                    chat_id,
                    raw_text,
                    "Мягко попроси написать сумму, которую клиент хочет получить.",
                    "Подскажите, пожалуйста, какую сумму вы хотите получить.",
                )
                sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
                self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                return True
            exchange["receive"] = {
                "amount": format_decimal(amount.copy_abs()),
                "currency": receive_currency,
                "raw": f"{format_decimal(amount.copy_abs())} {receive_currency}",
            }
            exchange["status"] = "collecting"
            self.chat_store.set_active_exchange(chat_id, exchange)

        if exchange.get("status") == "awaiting_amount_currency_clarification":
            target_currency = self._extract_target_currency(raw_text)
            pending_amount = exchange.get("pending_amount")
            if not target_currency or not pending_amount:
                reply = await self._exchange_reply(
                    chat_id,
                    raw_text,
                    "Коротко уточни, к какой валюте относится сумма, которую клиент уже написал.",
                    f"Подскажите, пожалуйста, {exchange.get('pending_amount', '')} чего именно?",
                )
                sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
                self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                return True
            if self._has_currency_only(exchange.get("receive")) and target_currency == exchange["receive"]["currency"]:
                exchange["receive"] = {
                    "amount": pending_amount,
                    "currency": target_currency,
                    "raw": f"{pending_amount} {exchange['receive'].get('display_currency') or target_currency}",
                    "display_currency": exchange["receive"].get("display_currency") or target_currency,
                }
            elif self._has_currency_only(exchange.get("give")) and target_currency == exchange["give"]["currency"]:
                exchange["give"] = {
                    "amount": pending_amount,
                    "currency": target_currency,
                    "raw": f"{pending_amount} {exchange['give'].get('display_currency') or target_currency}",
                    "display_currency": exchange["give"].get("display_currency") or target_currency,
                }
            elif exchange.get("receive") and not exchange.get("give"):
                exchange["give"] = {
                    "amount": pending_amount,
                    "currency": target_currency,
                    "raw": f"{pending_amount} {target_currency}",
                    "display_currency": target_currency,
                }
            elif exchange.get("give") and not exchange.get("receive"):
                exchange["receive"] = {
                    "amount": pending_amount,
                    "currency": target_currency,
                    "raw": f"{pending_amount} {target_currency}",
                    "display_currency": target_currency,
                }
            exchange.pop("pending_amount", None)
            exchange["status"] = "collecting"
            self.chat_store.set_active_exchange(chat_id, exchange)

        if exchange.get("status") == "awaiting_amount_choice":
            items = self._extract_exchange_items(raw_text)
            amount = items[0]["amount"] if items else None
            explicit_currency = items[0]["currency"] if items else None
            if amount is None:
                raw_amount = self._extract_amount_only(raw_text)
                amount = format_decimal(raw_amount.copy_abs()) if raw_amount is not None else None
            lowered = raw_text.casefold()
            has_give_hint = any(token in lowered for token in ("отда", "отдам", "плачу", "с меня"))
            has_receive_hint = any(token in lowered for token in ("получ", "хочу", "нужно", "надо"))
            if amount is None:
                reply = await self._exchange_reply(
                    chat_id,
                    raw_text,
                    "Мягко попроси написать одну сумму: либо сколько клиент отдаёт, либо сколько хочет получить.",
                    "Напишите, пожалуйста, одну сумму: либо сколько хотите отдать, либо сколько хотите получить.",
                )
                sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
                self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                return True
            give_currency = (exchange.get("give") or {}).get("currency")
            receive_currency = (exchange.get("receive") or {}).get("currency")
            if explicit_currency and explicit_currency == give_currency:
                exchange["give"] = {
                    "amount": amount,
                    "currency": give_currency,
                    "raw": f"{amount} {give_currency}",
                }
            elif explicit_currency and explicit_currency == receive_currency:
                exchange["receive"] = {
                    "amount": amount,
                    "currency": receive_currency,
                    "raw": f"{amount} {receive_currency}",
                }
            elif has_give_hint and not has_receive_hint:
                give_currency = (exchange.get("give") or {}).get("currency")
                exchange["give"] = {
                    "amount": amount,
                    "currency": give_currency,
                    "raw": f"{amount} {give_currency}",
                }
            else:
                exchange["receive"] = {
                    "amount": amount,
                    "currency": receive_currency,
                    "raw": f"{amount} {receive_currency}",
                }
            exchange["status"] = "collecting"
            self.chat_store.set_active_exchange(chat_id, exchange)

        amount_only = self._extract_amount_only(raw_text)
        items_in_text = self._extract_exchange_items(raw_text)
        if (
            exchange.get("status") == "collecting"
            and amount_only is not None
            and not items_in_text
            and (
                (self._has_currency_only(exchange.get("receive")) and not exchange.get("give"))
                or (self._has_currency_only(exchange.get("give")) and not exchange.get("receive"))
            )
        ):
            amount_text = format_decimal(amount_only.copy_abs())
            exchange["pending_amount"] = amount_text
            exchange["status"] = "awaiting_amount_currency_clarification"
            self.chat_store.set_active_exchange(chat_id, exchange)
            reply = await self._exchange_reply(
                chat_id,
                raw_text,
                f"Коротко уточни, к какой валюте относится сумма {amount_text}.",
                f"Подскажите, пожалуйста, {amount_text} чего именно?",
            )
            sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
            self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
            return True

        if exchange.get("give") is None:
            items = self._extract_exchange_items(raw_text)
            if items and seeded_side != "receive":
                exchange["give"] = items[0]
                self.chat_store.set_active_exchange(chat_id, exchange)
            elif seeded_side != "receive" and (source_currency := self._extract_target_currency(raw_text)):
                exchange["give"] = {"currency": source_currency, "raw": source_currency}
                self.chat_store.set_active_exchange(chat_id, exchange)
            else:
                reply = await self._exchange_reply(
                    chat_id,
                    raw_text,
                    "Коротко и естественно спроси у клиента, что он отдаёт для обмена.",
                    "Подскажите, пожалуйста, что вы отдаёте для обмена.",
                )
                sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
                self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                return True

        if exchange.get("receive") is None:
            items = self._extract_exchange_items(raw_text)
            if (
                items
                and seeded_side != "give"
                and (exchange.get("give") is None or items[0]["raw"] != exchange["give"]["raw"])
            ):
                exchange["receive"] = items[-1]
            elif seeded_side != "give" and (target_currency := self._extract_target_currency(raw_text)):
                exchange["receive"] = {"currency": target_currency, "raw": target_currency}
            self.chat_store.set_active_exchange(chat_id, exchange)
            if exchange.get("receive") is None:
                reply = await self._exchange_reply(
                    chat_id,
                    raw_text,
                    "Коротко и естественно спроси у клиента, что он хочет получить в обмен.",
                    "Подскажите, пожалуйста, что вы хотите получить.",
                )
                sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
                self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                return True

        if not self._exchange_ready_for_confirmation(exchange):
            if exchange.get("give") and not exchange.get("receive"):
                reply = await self._exchange_reply(
                    chat_id,
                    raw_text,
                    "Коротко спроси, какую валюту клиент хочет получить.",
                    "Подскажите, пожалуйста, какую валюту хотите получить.",
                )
                sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
                self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                return True
            if exchange.get("receive") and not exchange.get("give"):
                reply = await self._exchange_reply(
                    chat_id,
                    raw_text,
                    "Коротко спроси, какую валюту и сумму клиент готов отдать.",
                    "Подскажите, пожалуйста, что вы готовы отдать.",
                )
                sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
                self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                return True
            if self._has_currency_only(exchange.get("receive")) and self._has_currency_only(exchange.get("give")):
                exchange["status"] = "awaiting_amount_choice"
                self.chat_store.set_active_exchange(chat_id, exchange)
                reply = await self._exchange_reply(
                    chat_id,
                    raw_text,
                    (
                        f"Скажи, что для расчёта нужна одна сумма."
                        f" Спроси, что клиенту удобнее написать сейчас: сколько он хочет отдать в {exchange['give']['currency']}"
                        f" или сколько хочет получить в {exchange['receive']['currency']}."
                    ),
                    (
                        f"Чтобы рассчитать обмен, мне нужно понимать сумму. "
                        f"Напишите, пожалуйста, либо сколько хотите отдать в {exchange['give']['currency']}, "
                        f"либо сколько хотите получить в {exchange['receive']['currency']}."
                    ),
                )
                sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
                self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                return True

        exchange["status"] = "awaiting_confirmation"
        self.chat_store.set_active_exchange(chat_id, exchange)
        reply = self._build_exchange_summary(exchange)
        sent = await context.bot.send_message(
            chat_id=business_chat_id,
            text=reply,
            business_connection_id=business_connection_id,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Верно", callback_data=f"exchange|{exchange['id']}|ok"),
                InlineKeyboardButton("Исправить", callback_data=f"exchange|{exchange['id']}|edit"),
            ]]),
        )
        self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
        return True

    async def _greeting_text(self, chat_id: str) -> str:
        style = await self.repo.get_communication_style(chat_id)
        variants = await self.repo.get_greeting_variants(style)
        style_key = "friendly" if style == "Дружеский" else "business"
        index = self.chat_store.get_next_greeting_index(chat_id, style_key, len(variants))
        return variants[index]

    async def _short_greeting_text(self, chat_id: str) -> str:
        style = await self.repo.get_communication_style(chat_id)
        if style == "Дружеский":
            return "Привет!"
        return "Здравствуйте."

    async def _send_typing_delay(
        self,
        context: ContextTypes.DEFAULT_TYPE | None,
        *,
        chat_id: int,
        business_connection_id: str | None = None,
    ) -> None:
        if context is not None:
            kwargs = {"chat_id": chat_id, "action": ChatAction.TYPING}
            if business_connection_id:
                kwargs["business_connection_id"] = business_connection_id
            await context.bot.send_chat_action(**kwargs)
        await asyncio.sleep(5)

    async def _requests_status_sync_loop(self) -> None:
        while True:
            try:
                await self.repo.sync_request_status_colors()
            except Exception:
                LOGGER.exception("Failed to sync request status colors")
            await asyncio.sleep(30)

    async def _sync_dialog_examples_counts(self) -> None:
        for chat_id in self.chat_store.get_all_chat_ids():
            try:
                count = self.chat_store.count_style_examples(chat_id)
                if count == 0:
                    count = self.chat_store.count_dialog_history_entries(chat_id)
                await self.repo.update_dialog_examples_count(
                    chat_id,
                    count,
                )
            except Exception:
                LOGGER.exception("Failed to sync dialog examples count for %s", chat_id)

    async def _post_init(self, app) -> None:
        await self._sync_dialog_examples_counts()
        if self._status_sync_task is None or self._status_sync_task.done():
            self._status_sync_task = app.create_task(self._requests_status_sync_loop())

    async def _post_shutdown(self, _) -> None:
        if self._status_sync_task is not None:
            self._status_sync_task.cancel()
            self._status_sync_task = None

    async def _reply(
        self,
        message,
        text: str,
        rows: list[list[InlineKeyboardButton]] | None = None,
        with_menu: bool = True,
        context: ContextTypes.DEFAULT_TYPE | None = None,
        **kwargs,
    ):
        chat = getattr(message, "chat", None)
        if chat:
            await self._send_typing_delay(context, chat_id=chat.id)
        reply_markup = self._menu_markup(rows) if with_menu else None
        sent = await message.reply_text(text, reply_markup=reply_markup, **kwargs)
        if chat:
            chat_id = str(chat.id)
            chat_name = chat.title or chat.username or str(chat.id)
            self.chat_store.log_outgoing_text(chat_id, chat_name, text, getattr(sent, "message_id", None))
        return sent

    async def _edit(
        self,
        query,
        text: str,
        rows: list[list[InlineKeyboardButton]] | None = None,
        with_menu: bool = True,
        **kwargs,
    ):
        try:
            reply_markup = self._menu_markup(rows) if with_menu else None
            edited = await query.edit_message_text(text, reply_markup=reply_markup, **kwargs)
            chat = getattr(query.message, "chat", None)
            if chat:
                chat_id = str(chat.id)
                chat_name = chat.title or chat.username or str(chat.id)
                self.chat_store.log_outgoing_text(chat_id, chat_name, text, getattr(query.message, "message_id", None))
            return edited
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                return None
            raise

    async def _send_business_text(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        business_connection_id: str,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup=None,
    ):
        await self._send_typing_delay(
            context,
            chat_id=chat_id,
            business_connection_id=business_connection_id,
        )
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            business_connection_id=business_connection_id,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
        )
        return sent

    async def _build_ai_profile(self, chat_id: str, current_text: str = "") -> dict:
        profile = self.chat_store.load_profile(chat_id)
        profile["communication_style"] = await self.repo.get_communication_style(chat_id)
        profile["chat_profile"] = self.chat_store.load_chat_profile(chat_id)
        profile["current_text"] = current_text
        profile["style_examples"] = self.chat_store.get_similar_style_examples(chat_id, current_text, limit=8)
        return profile

    async def _send_greeting_with_delay(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return
        await self._reply(message, await self._greeting_text(str(chat.id)), with_menu=False, context=context)

    def _split_greeting_remainder(self, text: str) -> tuple[bool, str]:
        match = re.search(
            r"\b(привет|здравствуйте|здравствуй|добрый день|добрый вечер|доброе утро|доброго дня|hello|hi)\b",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return False, text.strip()
        remainder = (text[:match.start()] + " " + text[match.end():]).strip(" ,.;:!?-")
        return True, re.sub(r"\s+", " ", remainder).strip()

    async def _handle_greeting(self, update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str) -> bool:
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return False
        candidate_text = raw_text
        if chat.type != ChatType.PRIVATE:
            bot_username = await self._get_bot_username(context)
            if not bot_username or f"@{bot_username}".lower() not in raw_text.lower():
                return False
            candidate_text = self._strip_mention(raw_text, bot_username)
        if not looks_like_greeting(candidate_text):
            return False
        await self._send_greeting_with_delay(update, context)
        return True

    async def _handle_thanks(self, update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str) -> bool:
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return False
        candidate_text = raw_text
        if chat.type != ChatType.PRIVATE:
            bot_username = await self._get_bot_username(context)
            if not bot_username or f"@{bot_username}".lower() not in raw_text.lower():
                return False
            candidate_text = self._strip_mention(raw_text, bot_username)
        reply = await self.repo.get_thanks_reply(candidate_text)
        if not reply:
            return False
        await self._reply(message, reply, with_menu=False)
        return True

    def _format_rate_line(self, title: str, value: str) -> str:
        lowered = title.strip().lower()
        clean_value = value.strip().replace(".", ",")
        if lowered == "евро/доллар":
            return f"- Евро/доллар: {clean_value} $"
        if "usd" in lowered or "dollar" in lowered:
            return f"- Доллар: {clean_value} руб"
        if "eur" in lowered:
            return f"- Евро: {clean_value} руб"
        if "try" in lowered or "lira" in lowered or "lir" in lowered:
            return f"- Лира: {clean_value} руб"
        return f"- {title}: {clean_value}"

    def _short_currency_label(self, code: str) -> str:
        labels = {
            "RUB": "руб",
            "USD": "долларов",
            "USDT": "юсдт",
            "USDC": "usdc",
            "EUR": "евро",
            "TRY": "лир",
            "KZT": "тенге",
        }
        return labels.get(code, code)

    def _quote_closing_text(self, give_currency: str) -> str:
        if give_currency == "EUR":
            return "Если всё верно, подскажите, когда удобно будет передать евро?"
        return "Если готовы, подготовлю карту."

    async def _get_bot_username(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        if context.bot.username:
            return context.bot.username
        me = await context.bot.get_me()
        return me.username or ""

    def _is_slash_expression(self, text: str) -> bool:
        return bool(re.match(r"^/\s*[=()0-9+\-]", text.strip()))

    def _should_process(self, chat_type: str, text: str, bot_username: str) -> bool:
        if chat_type == ChatType.PRIVATE:
            return self._is_slash_expression(text)
        if not bot_username:
            return False
        return f"@{bot_username}".lower() in text.lower()

    def _strip_mention(self, text: str, bot_username: str) -> str:
        if not bot_username:
            return text.strip()
        return re.sub(rf"@{re.escape(bot_username)}\b", "", text, count=1, flags=re.IGNORECASE).strip()

    def _normalize_expression_text(self, chat_type: str, text: str, bot_username: str) -> str:
        stripped = self._strip_mention(text, bot_username)
        if chat_type == ChatType.PRIVATE and stripped.startswith("/"):
            return stripped[1:].strip()
        return stripped

    def _extract_currency(self, text: str):
        trimmed = text.strip()
        if not trimmed:
            return "", None
        match = re.search(r"([A-Za-zА-Яа-я]+)\s*$", trimmed)
        if not match:
            return trimmed, None
        token = match.group(1).lower()
        if token in ALIAS_TO_CURRENCY:
            return trimmed[: match.start()].strip(), ALIAS_TO_CURRENCY[token]
        allowed = ", ".join(sorted({alias for aliases in SUPPORTED_CURRENCY_ALIASES.values() for alias in aliases}))
        raise ValueError(f"Неизвестная валюта '{match.group(1)}'. Используй одно из: {allowed}")

    def _parse_manual_value(self, fragment: str):
        cleaned = fragment.strip()
        multiplier = Decimal("1000") if cleaned.lower().endswith(("к", "k")) else Decimal("1")
        if multiplier != Decimal("1"):
            cleaned = cleaned[:-1]
        cleaned = cleaned.replace("\u00A0", "").replace("\u202F", "").replace(" ", "").replace("'", "")
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", cleaned):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", cleaned):
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")
        cleaned = re.sub(r"[^0-9+\-.]", "", cleaned)
        if not cleaned:
            return None
        try:
            return Decimal(cleaned) * multiplier
        except Exception:
            return None

    def _build_receipt_row(self, entry: PendingReceiptEntry, amount: Decimal) -> SheetRow:
        return SheetRow(
            chat_id=entry.target_chat_id,
            chat_name=entry.target_chat_name,
            user=entry.user_name,
            expression=f"Чек {entry.currency} -{format_decimal(amount)} ({entry.filename})",
            delta=-amount,
            timestamp=datetime.now(timezone.utc).isoformat(),
            currency=entry.currency,
        )

    def _looks_like_command_or_text(self, text: str) -> bool:
        return bool(text.strip())

    async def handle_business_connection(self, update: Update, _: ContextTypes.DEFAULT_TYPE):
        connection = update.business_connection
        if not connection:
            return
        self.chat_store.set_business_connection(connection.id, connection.user.id, connection.user_chat_id)

    async def handle_business_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.business_message or update.edited_business_message
        if not message:
            return
        if getattr(message, "sender_business_bot", None):
            return
        chat = message.chat
        user = message.from_user
        if not chat or not user:
            return
        chat_id = str(chat.id)
        chat_name = chat.title or chat.username or user.full_name or chat_id
        user_name = user.full_name or user.username or str(user.id)
        await self.repo.ensure_row(chat_id, chat_name, user_name)
        self.chat_store.ensure_profile(chat_id, chat_name, user_name)
        self.chat_store.ensure_chat_profile(chat_id, chat_name, user_name)
        connection_id = message.business_connection_id or ""
        connection_meta = self.chat_store.get_business_connection(connection_id) if connection_id else {}
        owner_user_id = connection_meta.get("user_id")
        raw_text = (message.text or "").strip()
        if raw_text:
            self.chat_store.log_incoming_text_entry(
                chat_id,
                chat_name,
                user.id,
                user_name,
                raw_text,
                message_id=message.message_id,
                source="business",
            )
        is_operator_message = owner_user_id == user.id or (
            bool(connection_id) and str(user.id) != chat_id
        )
        if is_operator_message and raw_text:
            last_client_text = self.chat_store.load_chat_profile(chat_id).get("last_client_text", "")
            self.chat_store.append_style_example(
                chat_id,
                chat_name=chat_name,
                client_text=last_client_text,
                manual_reply=raw_text,
                user_name=user_name,
                message_id=message.message_id,
            )
            await self.repo.update_dialog_examples_count(
                chat_id,
                max(
                    self.chat_store.count_style_examples(chat_id),
                    self.chat_store.count_dialog_history_entries(chat_id),
                ),
            )
            return
        if owner_user_id != user.id and not await self.repo.is_verified_chat(chat_id):
            return
        greeting_found, greeting_remainder = self._split_greeting_remainder(raw_text)
        if owner_user_id != user.id and greeting_found:
            reply = await self._short_greeting_text(chat_id) if greeting_remainder else await self._greeting_text(chat_id)
            sent = await self._send_business_text(context, chat.id, connection_id, reply)
            self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
            if not greeting_remainder:
                return
            raw_text = greeting_remainder
        if owner_user_id != user.id:
            reply = await self.repo.get_thanks_reply(raw_text)
            if not reply:
                pass
            else:
                sent = await self._send_business_text(context, chat.id, connection_id, reply)
                self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
                return
        if owner_user_id != user.id and raw_text:
            if await self._handle_exchange_flow(
                chat_id,
                raw_text,
                context,
                business_chat_id=chat.id,
                business_connection_id=connection_id,
                chat_name=chat_name,
            ):
                return

    async def start(self, update: Update, _: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        user = update.effective_user
        chat_name = chat.title or chat.username or user.full_name or str(chat.id)
        user_name = user.full_name or user.username or str(user.id)
        await self.repo.ensure_row(str(chat.id), chat_name, user_name)
        self.chat_store.ensure_profile(str(chat.id), chat_name, user_name)
        await self.show_menu(update, _)

    async def show_menu(self, update: Update, _: ContextTypes.DEFAULT_TYPE):
        await self._reply(update.effective_message, self._menu_text(), rows=self._menu_rows(), parse_mode="Markdown")

    async def send_total(self, update: Update, _: ContextTypes.DEFAULT_TYPE):
        return await self._send_total_for_chat(update.effective_message, str(update.effective_chat.id))

    async def _send_total_for_chat(self, message, chat_id: str):
        totals = await self.repo.get_currency_totals(chat_id)
        lines = []
        for code in SUPPORTED_CURRENCY_ORDER:
            value = totals.get(code, Decimal("0"))
            if value != Decimal("0"):
                lines.append(f"- {code}: {format_decimal(value)}")
        note = "\n\nБаланс:\n«-» клиент должен\n«+» фин услуга должна."
        if not lines:
            return await self._reply(message, f"Остатки: все валюты = 0{note}")
        return await self._reply(message, "Остатки:\n" + "\n".join(lines) + note)

    async def _edit_total_for_chat(self, query, chat_id: str):
        totals = await self.repo.get_currency_totals(chat_id)
        lines = []
        for code in SUPPORTED_CURRENCY_ORDER:
            value = totals.get(code, Decimal("0"))
            if value != Decimal("0"):
                lines.append(f"- {code}: {format_decimal(value)}")
        note = "\n\nБаланс:\n«-» клиент должен\n«+» фин услуга должна."
        if not lines:
            return await self._edit(query, f"Остатки: все валюты = 0{note}")
        return await self._edit(query, "Остатки:\n" + "\n".join(lines) + note)

    async def send_rates(self, update: Update, _: ContextTypes.DEFAULT_TYPE):
        entries = await self.repo.get_rate_values(str(update.effective_chat.id))
        if not entries:
            return await self._reply(update.effective_message, "Для этого чата нет столбцов с курсом (rate).")
        lines = [self._format_rate_line(title, value) for title, value in entries]
        return await self._reply(update.effective_message, "Курсы:\n" + "\n".join(lines))

    async def _edit_rates_for_chat(self, query, chat_id: str):
        entries = await self.repo.get_rate_values(chat_id)
        if not entries:
            return await self._edit(query, "Для этого чата нет столбцов с курсом (rate).")
        lines = [self._format_rate_line(title, value) for title, value in entries]
        return await self._edit(query, "Курсы:\n" + "\n".join(lines))

    async def _process_expression(self, update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str):
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        bot_username = await self._get_bot_username(context)
        if not self._should_process(chat.type, raw_text, bot_username):
            return
        stripped_text = self._normalize_expression_text(chat.type, raw_text, bot_username)
        if not stripped_text or self._is_known_command_text(raw_text):
            return
        if not looks_like_expression(stripped_text):
            return
        try:
            text, currency = self._extract_currency(stripped_text)
        except ValueError as exc:
            await self._reply(message, str(exc))
            return
        if not text:
            await self._reply(message, "Добавь выражение перед валютой.")
            return
        if currency is None:
            return
        manual_value = None
        if "=" in text:
            _, right = text.split("=", 1)
            manual_value = self._parse_manual_value(right)
            if manual_value is None:
                await self._reply(message, "Не могу прочитать число после '='.")
                return
        try:
            result_decimal = manual_value if manual_value is not None else Decimal(str(self.evaluator.evaluate(text)))
        except Exception as exc:
            await self._reply(message, f"Ошибка: {exc}")
            return
        row = SheetRow(
            chat_id=str(chat.id),
            chat_name=chat.title or chat.username or user.full_name or str(chat.id),
            user=user.full_name or user.username or str(user.id),
            expression=stripped_text,
            delta=result_decimal,
            timestamp=datetime.now(timezone.utc).isoformat(),
            currency=currency,
        )
        total = await self.repo.upsert(row)
        symbol = "⇒" if manual_value is not None else "="
        await self._reply(message, f"{stripped_text} {symbol} {format_decimal(result_decimal)} (сумма: {format_decimal(total)})")
        await self.send_total(update, context)

    async def _register_receipt_amount(self, entry: PendingReceiptEntry, amount: Decimal, reply_target):
        total = await self.repo.upsert(self._build_receipt_row(entry, amount))
        self.pending_receipts.pop(entry.token, None)
        await self._reply(
            reply_target,
            f"Чек сохранён: -{format_decimal(amount)} {entry.currency}. Текущий итог по {entry.currency}: {format_decimal(total)}"
        )
        await self._send_total_for_chat(reply_target, entry.target_chat_id)
        return total

    async def _handle_menu_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str) -> bool:
        text = raw_text.strip().casefold()
        if text.startswith("/menu") or text in {"меню", "menu"}:
            await self.show_menu(update, context)
            return True
        if text.startswith("/summa") or text.startswith("/total") or text in {"сумма", "summa", "total", "баланс", "balance"}:
            await self.send_total(update, context)
            return True
        if text.startswith("/kurs") or text.startswith("/курс") or text in {"курс", "kurs"}:
            await self.send_rates(update, context)
            return True
        return False

    async def handle_expression(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.effective_message
        if not message:
            return
        if getattr(message, "business_connection_id", None):
            return
        self.chat_store.log_incoming_text(update)
        if await self._handle_manual_receipt_amount(update, context):
            return
        raw_text = (message.text or "").strip()
        if not raw_text:
            return
        greeting_found, greeting_remainder = self._split_greeting_remainder(raw_text)
        if greeting_found:
            await self._send_greeting_with_delay(update, context)
            if not greeting_remainder:
                return
            raw_text = greeting_remainder
        if await self._handle_thanks(update, context, raw_text):
            return
        if await self._handle_menu_command(update, context, raw_text):
            return
        if self._is_known_command_text(raw_text):
            return
        await self._process_expression(update, context, raw_text)

    async def handle_receipt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.effective_message
        chat = update.effective_chat
        if not message:
            return
        bot_username = await self._get_bot_username(context)
        if chat.type != ChatType.PRIVATE:
            caption = (message.caption or "").lower()
            if not bot_username or f"@{bot_username.lower()}" not in caption:
                return
        tg_file = None
        filename = None
        if message.photo:
            tg_file = await message.photo[-1].get_file()
            filename = f"{tg_file.file_unique_id}.jpg"
        elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
            tg_file = await message.document.get_file()
            filename = message.document.file_name or f"{tg_file.file_unique_id}"
        else:
            return
        local_path = self.receipts_dir / filename
        await tg_file.download_to_drive(str(local_path))
        user = update.effective_user
        self.chat_store.ensure_profile(
            str(chat.id),
            chat.title or chat.username or user.full_name or str(chat.id),
            user.full_name or user.username or str(user.id),
        )
        self.chat_store.save_attachment_copy(
            str(chat.id),
            str(local_path),
            original_name=filename,
            media_kind="receipt",
            message_id=message.message_id,
            caption=message.caption or "",
            user_id=user.id if user else None,
            user_name=user.full_name or user.username or str(user.id) if user else "",
        )
        try:
            receipt = self.receipt_processor.parse(str(local_path))
        except Exception as exc:
            await self._reply(message, f"Не смог прочитать чек: {exc}")
            return
        entry = PendingReceiptEntry(
            token=uuid4().hex,
            target_chat_id=str(chat.id),
            target_chat_name=chat.title or chat.username or user.full_name or str(chat.id),
            user_name=user.full_name or user.username or str(user.id),
            currency=receipt.currency,
            amount=receipt.amount.copy_abs(),
            filename=filename,
        )
        self.pending_receipts[entry.token] = entry
        await self._reply(
            message,
            f"Распознано {format_decimal(entry.amount)} {entry.currency}. Всё верно?",
            rows=[[
                InlineKeyboardButton("Верно", callback_data=f"receipt|{entry.token}|ok"),
                InlineKeyboardButton("Исправить", callback_data=f"receipt|{entry.token}|edit"),
            ]],
        )

    async def handle_attachment_archive(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not message or not chat or not user:
            return
        if message.photo or (message.document and message.document.mime_type and message.document.mime_type.startswith("image/")):
            return
        tg_file = None
        filename = None
        media_kind = None
        if message.document:
            tg_file = await message.document.get_file()
            filename = message.document.file_name or f"{tg_file.file_unique_id}"
            media_kind = "document"
        elif message.video:
            tg_file = await message.video.get_file()
            filename = message.video.file_name or f"{tg_file.file_unique_id}.mp4"
            media_kind = "video"
        elif message.voice:
            tg_file = await message.voice.get_file()
            filename = f"{tg_file.file_unique_id}.ogg"
            media_kind = "voice"
        elif message.audio:
            tg_file = await message.audio.get_file()
            filename = message.audio.file_name or f"{tg_file.file_unique_id}.mp3"
            media_kind = "audio"
        if not tg_file or not filename or not media_kind:
            return
        target_dir = self.receipts_dir / "attachments"
        target_dir.mkdir(parents=True, exist_ok=True)
        local_path = target_dir / filename
        await tg_file.download_to_drive(str(local_path))
        self.chat_store.ensure_profile(
            str(chat.id),
            chat.title or chat.username or user.full_name or str(chat.id),
            user.full_name or user.username or str(user.id),
        )
        self.chat_store.save_attachment_copy(
            str(chat.id),
            str(local_path),
            original_name=filename,
            media_kind=media_kind,
            message_id=message.message_id,
            caption=message.caption or "",
            user_id=user.id,
            user_name=user.full_name or user.username or str(user.id),
        )
        if media_kind in {"voice", "audio"}:
            transcript = self.audio_transcriber.transcribe(str(local_path))
            if transcript:
                chat_id = str(chat.id)
                chat_name = chat.title or chat.username or user.full_name or chat_id
                user_name = user.full_name or user.username or str(user.id)
                self.chat_store.log_incoming_text_entry(
                    chat_id,
                    chat_name,
                    user.id,
                    user_name,
                    transcript,
                    message_id=message.message_id,
                    source="voice_transcript",
                )
                if getattr(message, "business_connection_id", None):
                    await self._handle_transcribed_business_text(
                        chat_id,
                        chat.id,
                        chat_name,
                        user,
                        message.business_connection_id,
                        transcript,
                        context,
                    )

    async def _handle_transcribed_business_text(
        self,
        chat_id: str,
        business_chat_id: int,
        chat_name: str,
        user,
        business_connection_id: str,
        transcript: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        connection_meta = self.chat_store.get_business_connection(business_connection_id) if business_connection_id else {}
        owner_user_id = connection_meta.get("user_id")
        if owner_user_id == user.id:
            return
        if not await self.repo.is_verified_chat(chat_id):
            return
        greeting_found, greeting_remainder = self._split_greeting_remainder(transcript)
        raw_text = transcript
        if greeting_found:
            reply = await self._short_greeting_text(chat_id) if greeting_remainder else await self._greeting_text(chat_id)
            sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
            self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
            if not greeting_remainder:
                return
            raw_text = greeting_remainder
        reply = await self.repo.get_thanks_reply(raw_text)
        if reply:
            sent = await self._send_business_text(context, business_chat_id, business_connection_id, reply)
            self.chat_store.log_outgoing_text(chat_id, chat_name, reply, getattr(sent, "message_id", None))
            return
        await self._handle_exchange_flow(
            chat_id,
            raw_text,
            context,
            business_chat_id=business_chat_id,
            business_connection_id=business_connection_id,
            chat_name=chat_name,
        )

    async def handle_receipt_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query or not query.data:
            return
        parts = query.data.split("|", 2)
        if len(parts) != 3:
            await query.answer("Некорректный ответ", show_alert=True)
            return
        kind, middle, action = parts
        if kind == "menu":
            if action == "open":
                await query.answer()
                await self._edit(query, self._menu_text(), rows=self._menu_rows(), parse_mode="Markdown")
                return
            if action == "summa":
                await query.answer()
                await self._edit_total_for_chat(query, str(update.effective_chat.id))
                return
            if action == "kurs":
                await query.answer()
                await self._edit_rates_for_chat(query, str(update.effective_chat.id))
                return
            await query.answer("Неизвестная кнопка", show_alert=True)
            return
        if kind == "exchange":
            await query.answer()
            chat_id = str(update.effective_chat.id)
            if action in {"try_cash", "try_card"}:
                request_number = int(middle)
                await self.repo.update_exchange_request_status(request_number, "готовы")
                if action == "try_cash":
                    await self._edit(
                        query,
                        "Заявка принята. Скоро отправлю адрес.",
                        with_menu=False,
                    )
                else:
                    await self._edit(
                        query,
                        "Заявка принята, скоро выдам карту.",
                        with_menu=False,
                    )
                return
            if action in {"ready", "not_ready"}:
                request_number = int(middle)
                status = "готовы" if action == "ready" else "не готовы"
                await self.repo.update_exchange_request_status(request_number, status)
                ready_text = "Принял в работу, скоро выдам карту."
                source_text = (getattr(query.message, "text", "") or "").casefold()
                if action == "ready" and re.search(r"чтобы получить\s+\S+\s+руб", source_text):
                    ready_text = "Ждём реквизиты:\nТелефон\nФИО\nБанки"
                elif action == "ready" and "руб" in source_text:
                    ready_text = "Принял в работу, скоро выдам карту под рубли."
                await self._edit(
                    query,
                    ready_text if action == "ready" else "Хорошо, если что напишите.",
                    with_menu=False,
                )
                return
            exchange = self.chat_store.get_active_exchange(chat_id)
            if not exchange or exchange.get("id") != middle:
                await self._edit(query, "Эта заявка уже неактуальна.", with_menu=False)
                return
            if action == "edit":
                exchange["status"] = "awaiting_edit_choice"
                self.chat_store.set_active_exchange(chat_id, exchange)
                await self._edit(query, "Что нужно исправить: `отдаёт` или `получает`?", with_menu=False, parse_mode="Markdown")
                return
            if action == "ok":
                if not exchange.get("give") or not exchange.get("receive"):
                    await self._edit(query, "Заявка неполная, нужно собрать её заново.", with_menu=False)
                    self.chat_store.set_active_exchange(chat_id, None)
                    return
                quote_text = ""
                followup_text = ""
                give_amount = Decimal(exchange["give"]["amount"]) if exchange["give"].get("amount") else None
                receive_amount = Decimal(exchange["receive"]["amount"]) if exchange["receive"].get("amount") else None
                if give_amount is not None and receive_amount is None:
                    quote = await self.repo.get_market_quote(
                        exchange["give"]["currency"],
                        exchange["receive"]["currency"],
                    )
                    if quote is not None:
                        rate, mode = quote
                        if rate != Decimal("0"):
                            calculated_receive = give_amount * rate if mode == "give_times_rate" else give_amount / rate
                            rounded_receive = round_exchange_amount(calculated_receive, exchange["receive"]["currency"])
                            receive_value = format_decimal(rounded_receive)
                            exchange["receive"]["amount"] = receive_value
                            receive_display = self._display_currency_code(exchange["receive"])
                            give_display = self._display_currency_code(exchange["give"])
                            exchange["receive"]["raw"] = f"{receive_value} {receive_display}"
                            operation = "×" if mode == "give_times_rate" else "/"
                            quote_text = (
                                f"\nРасчёт: {format_decimal(give_amount)} {give_display} {operation} "
                                f"{format_decimal(rate)} = {receive_value} {receive_display}\n"
                            )
                            followup_text = (
                                f"Чтобы получить {receive_value} {self._short_currency_label(receive_display)}, "
                                f"нужно {format_decimal(give_amount)} {self._short_currency_label(give_display)}. "
                                f"Расчёт: {format_decimal(give_amount)} {operation} {format_decimal(rate)} = {receive_value}. "
                                f"{self._quote_closing_text(exchange['give']['currency'])}"
                            )
                        else:
                            followup_text = ""
                    else:
                        followup_text = ""
                elif receive_amount is not None and give_amount is None:
                    quote = await self.repo.get_market_quote(
                        exchange["give"]["currency"],
                        exchange["receive"]["currency"],
                    )
                    if quote is not None:
                        rate, mode = quote
                        if rate != Decimal("0"):
                            calculated_give = receive_amount / rate if mode == "give_times_rate" else receive_amount * rate
                            rounded_give = round_exchange_amount(calculated_give, exchange["give"]["currency"])
                            give_value = format_decimal(rounded_give)
                            exchange["give"]["amount"] = give_value
                            give_display = self._display_currency_code(exchange["give"])
                            receive_display = self._display_currency_code(exchange["receive"])
                            exchange["give"]["raw"] = f"{give_value} {give_display}"
                            quote_text = (
                                f"\nРасчёт: {format_decimal(receive_amount)} {receive_display} "
                                f"{'/' if mode == 'give_times_rate' else '×'} {format_decimal(rate)} = "
                                f"{give_value} {give_display}\n"
                            )
                            followup_text = (
                                f"Чтобы получить {format_decimal(receive_amount)} {self._short_currency_label(receive_display)}, "
                                f"нужно {give_value} {self._short_currency_label(give_display)}. "
                                f"Расчёт: {format_decimal(receive_amount)} {'/' if mode == 'give_times_rate' else '×'} {format_decimal(rate)} = {give_value}. "
                                f"{self._quote_closing_text(exchange['give']['currency'])}"
                            )
                request_row = ExchangeRequestRow(
                    status="ожидание",
                    created_at=datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S"),
                    request_id=exchange["id"],
                    chat_id=chat_id,
                    chat_name=update.effective_chat.title or update.effective_chat.username or chat_id,
                    give_currency=exchange["give"]["currency"],
                    give_amount=Decimal(exchange["give"]["amount"]) if exchange["give"].get("amount") else None,
                    receive_currency=exchange["receive"]["currency"],
                    receive_amount=Decimal(exchange["receive"]["amount"]) if exchange["receive"].get("amount") else None,
                )
                request_number = await self.repo.append_exchange_request(request_row)
                self.chat_store.append_exchange_request(chat_id, {
                    "id": request_row.request_id,
                    "number": request_number,
                    "created_at": request_row.created_at,
                    "status": request_row.status,
                    "give": exchange["give"],
                    "receive": exchange["receive"],
                })
                self._remember_exchange_pattern(chat_id, exchange)
                self.chat_store.update_chat_profile_from_exchange(
                    chat_id,
                    exchange["give"]["currency"],
                    exchange["receive"]["currency"],
                )
                self.chat_store.set_active_exchange(chat_id, None)
                business_connection_id = getattr(query.message, "business_connection_id", None)
                if followup_text and business_connection_id:
                    followup_buttons = [[
                        InlineKeyboardButton("Готовы", callback_data=f"exchange|{request_number}|ready"),
                        InlineKeyboardButton("не готовы", callback_data=f"exchange|{request_number}|not_ready"),
                    ]]
                    if exchange["give"]["currency"] == "TRY" and exchange["receive"]["currency"] == "RUB":
                        followup_text = (
                            f"Чтобы получить {format_decimal(receive_amount or Decimal(exchange['receive']['amount']))} {self._short_currency_label(self._display_currency_code(exchange['receive']))}, "
                            f"нужно {exchange['give']['amount']} {self._short_currency_label(self._display_currency_code(exchange['give']))}. "
                            f"Расчёт: {exchange['give']['amount']} × {format_decimal(rate)} = {format_decimal(receive_amount or Decimal(exchange['receive']['amount']))}. "
                            "Лиры хотите отдать наличными или на карту?"
                        )
                        followup_buttons = [[
                            InlineKeyboardButton("Наличные", callback_data=f"exchange|{request_number}|try_cash"),
                            InlineKeyboardButton("На карту", callback_data=f"exchange|{request_number}|try_card"),
                        ]]
                    sent = await self._send_business_text(
                        context,
                        update.effective_chat.id,
                        business_connection_id,
                        followup_text,
                        reply_markup=InlineKeyboardMarkup(followup_buttons),
                    )
                    self.chat_store.log_outgoing_text(chat_id, request_row.chat_name, followup_text, getattr(sent, "message_id", None))
                    try:
                        await query.message.delete()
                    except Exception:
                        pass
                else:
                    await self._edit(
                        query,
                        f"Заявка №{request_number} принята.\n"
                        f"Отдаёте: {exchange['give']['raw']}\n"
                        f"Получаете: {exchange['receive']['raw']}\n"
                        f"{quote_text}\n"
                        "Скоро вернусь, сейчас посчитаю.",
                        with_menu=False,
                    )
                return
            await self._edit(query, "Неизвестное действие по заявке.", with_menu=False)
            return
        entry = self.pending_receipts.get(middle)
        if not entry:
            await query.answer("Эта проверка уже завершена", show_alert=True)
            return
        if action == "ok":
            await query.answer("Сохраняю чек…")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await self._register_receipt_amount(entry, entry.amount, query.message)
        elif action == "edit":
            await query.answer("Укажи сумму вручную")
            self.manual_receipt_inputs[query.from_user.id] = middle
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await self._reply(query.message, "Напиши сумму чека цифрами (пример: 12345.67)")
        else:
            await query.answer("Неизвестное действие", show_alert=True)

    async def _handle_manual_receipt_amount(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> bool:
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return False
        token = self.manual_receipt_inputs.get(user.id)
        if not token:
            return False
        entry = self.pending_receipts.get(token)
        if not entry:
            self.manual_receipt_inputs.pop(user.id, None)
            await self._reply(message, "Эта проверка уже завершена")
            return True
        manual_value = self._parse_manual_value(message.text or "")
        if manual_value is None:
            await self._reply(message, "Не могу прочитать сумму. Напиши цифрами, например 12345.67")
            return True
        entry.amount = manual_value.copy_abs()
        await self._register_receipt_amount(entry, entry.amount, message)
        self.manual_receipt_inputs.pop(user.id, None)
        return True

    def run(self):
        app = ApplicationBuilder().token(self.token).post_init(self._post_init).post_shutdown(self._post_shutdown).build()
        app.add_handler(BusinessConnectionHandler(self.handle_business_connection))
        app.add_handler(TypeHandler(Update, self.handle_business_update), group=-1)
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("menu", self.show_menu))
        app.add_handler(CommandHandler(["summa", "total"], self.send_total))
        app.add_handler(MessageHandler(filters.Regex(r"^/сумма(?:@[\w_]+)?\b"), self.send_total))
        app.add_handler(CommandHandler(["kurs"], self.send_rates))
        app.add_handler(MessageHandler(filters.Regex(r"^/курс(?:@[\w_]+)?\b"), self.send_rates))
        app.add_handler(CallbackQueryHandler(self.handle_receipt_callback, pattern=r"^(receipt|menu|exchange)\|"))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, self.handle_receipt))
        app.add_handler(MessageHandler(filters.ATTACHMENT, self.handle_attachment_archive))
        app.add_handler(MessageHandler(filters.TEXT, self.handle_expression))
        app.run_polling()
