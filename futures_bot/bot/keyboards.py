"""Inline keyboards (Russian UI to match the crypto bot's UX).

Callback-data strings are short to fit inside Telegram's 64-byte
limit even when concatenated with a numeric id. The picker / confirm
flow for /cancel mirrors the crypto bot exactly so muscle memory
carries over.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

if TYPE_CHECKING:
    from futures_bot.db.models import Block

# --- side / confirm (shared by all FSMs) ---
CB_SIDE_BUY = "side:buy"
CB_SIDE_SELL = "side:sell"
CB_CONFIRM = "confirm:yes"
CB_CANCEL = "confirm:no"

# --- main menu ---
CB_MENU_BLOCK = "menu:block"
CB_MENU_STATS = "menu:stats"
CB_MENU_BALANCE = "menu:balance"
CB_MENU_BACK = "menu:back"

# --- block submenu ---
CB_BLOCK_CREATE = "block:create"
CB_BLOCK_LIST = "block:list"
CB_BLOCK_CANCEL = "block:cancel"

# --- /cancel picker / confirm flow ---
CB_CANCEL_BLOCK_PICK = "cb:pick:"        # + <block_id>
CB_CANCEL_BLOCK_CONFIRM = "cb:confirm:"  # + <block_id>


def side_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 BUY (лонг)", callback_data=CB_SIDE_BUY),
                InlineKeyboardButton(text="🔴 SELL (шорт)", callback_data=CB_SIDE_SELL),
            ]
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=CB_CONFIRM),
                InlineKeyboardButton(text="❌ Отмена", callback_data=CB_CANCEL),
            ]
        ]
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Top-level menu: Блок / Статистика / Баланс."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Блок", callback_data=CB_MENU_BLOCK)],
            [InlineKeyboardButton(text="📊 Статистика", callback_data=CB_MENU_STATS)],
            [InlineKeyboardButton(text="💰 Баланс", callback_data=CB_MENU_BALANCE)],
        ]
    )


def block_submenu_keyboard() -> InlineKeyboardMarkup:
    """Block submenu — Создать / Активные / Отменить / Назад.

    No /modify equivalent yet: futures-side cancel-price is set once
    and the trader prefers /cancel + new block to mid-flight tweaks.
    Easy to add later if that changes.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать", callback_data=CB_BLOCK_CREATE)],
            [InlineKeyboardButton(text="📋 Активные блоки", callback_data=CB_BLOCK_LIST)],
            [InlineKeyboardButton(text="✋ Отменить", callback_data=CB_BLOCK_CANCEL)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_MENU_BACK)],
        ]
    )


def cancel_block_picker_keyboard(blocks: "Iterable[Block]") -> InlineKeyboardMarkup:
    """One row per active block + a Back row."""
    rows: list[list[InlineKeyboardButton]] = []
    for b in blocks:
        rows.append([
            InlineKeyboardButton(
                text=f"#{b.id}  {b.symbol} {b.side}  ({b.status})",
                callback_data=f"{CB_CANCEL_BLOCK_PICK}{b.id}",
            )
        ])
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_MENU_BLOCK)
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_block_confirm_keyboard(block_id: int) -> InlineKeyboardMarkup:
    """Two-button confirmation: Yes-close or Cancel-back-to-picker."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Да, закрыть",
                callback_data=f"{CB_CANCEL_BLOCK_CONFIRM}{block_id}",
            ),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CB_BLOCK_CANCEL),
        ]]
    )
