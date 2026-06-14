"""Reusable inline keyboards (Russian UI).

The menu structure mirrors the spec the trader laid out:

* Блок (submenu) → Создать / Активные блоки / Отменить / Изменить / Назад
* Статистика (submenu) → Сегодня / 7д / 30д / 3мес / 6мес / 1г / Все / Свой период / Назад
* Баланс
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

if TYPE_CHECKING:
    from src.db import Block

# --- side / confirm callbacks (used by /newblock, /track and /fib FSMs) ---
CB_SIDE_BUY = "side:buy"
CB_SIDE_SELL = "side:sell"
CB_CONFIRM = "confirm:yes"
CB_CANCEL = "confirm:no"

# --- main menu callbacks ---
CB_MENU_BLOCK = "menu:block"
CB_MENU_STATS = "menu:stats"
CB_MENU_BALANCE = "menu:balance"
CB_MENU_BACK = "menu:back"

# --- block submenu callbacks ---
CB_BLOCK_CREATE = "block:create"
CB_BLOCK_LIST = "block:list"
CB_BLOCK_CANCEL = "block:cancel"
CB_BLOCK_MODIFY = "block:modify"

# --- cancel-block interactive flow callbacks ---
# Two distinct prefixes so a startswith() filter on PICK never matches
# CONFIRM and vice versa. Both encode the block id as the suffix.
CB_CANCEL_BLOCK_PICK = "cb:pick:"        # + <block_id>
CB_CANCEL_BLOCK_CONFIRM = "cb:confirm:"  # + <block_id>

# --- stats time-range callbacks (days; "0" means all-time) ---
CB_STATS_TODAY = "stats:1"
CB_STATS_7D = "stats:7"
CB_STATS_30D = "stats:30"
CB_STATS_3MO = "stats:90"
CB_STATS_6MO = "stats:180"
CB_STATS_1Y = "stats:365"
CB_STATS_ALL = "stats:0"
CB_STATS_CUSTOM = "stats:custom"


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
    """Top-level menu shown by /menu — Блок / Статистика / Баланс."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Блок", callback_data=CB_MENU_BLOCK)],
            [InlineKeyboardButton(text="📊 Статистика", callback_data=CB_MENU_STATS)],
            [InlineKeyboardButton(text="💰 Баланс", callback_data=CB_MENU_BALANCE)],
        ]
    )


def block_submenu_keyboard() -> InlineKeyboardMarkup:
    """Block submenu — Создать / Активные блоки / Отменить / Изменить."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать", callback_data=CB_BLOCK_CREATE)],
            [InlineKeyboardButton(text="📋 Активные блоки", callback_data=CB_BLOCK_LIST)],
            [InlineKeyboardButton(text="✋ Отменить", callback_data=CB_BLOCK_CANCEL)],
            [InlineKeyboardButton(text="✏️ Изменить", callback_data=CB_BLOCK_MODIFY)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_MENU_BACK)],
        ]
    )


def cancel_block_picker_keyboard(blocks: "Iterable[Block]") -> InlineKeyboardMarkup:
    """One row per active block + a Back row.

    Each block row's callback_data is ``CB_CANCEL_BLOCK_PICK + str(block_id)``
    so the handler can split on the prefix and recover the integer id.
    Back goes to the block submenu (parent), not the main menu, so the
    trader doesn't have to traverse two levels to reach 'Отменить' again.
    """
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
    """Two-button confirmation: Yes-close or Cancel-back-to-picker.

    'Отмена' fires CB_BLOCK_CANCEL, which re-renders the picker — that
    way the trader can pick a different block in one click rather than
    bouncing up to the block submenu.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Да, закрыть",
                callback_data=f"{CB_CANCEL_BLOCK_CONFIRM}{block_id}",
            ),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CB_BLOCK_CANCEL),
        ]]
    )


def stats_window_keyboard() -> InlineKeyboardMarkup:
    """Time-window picker — Сегодня / 7д / 30д / 3мес / 6мес / 1г / Все / Свой период."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data=CB_STATS_TODAY),
                InlineKeyboardButton(text="7д", callback_data=CB_STATS_7D),
                InlineKeyboardButton(text="30д", callback_data=CB_STATS_30D),
            ],
            [
                InlineKeyboardButton(text="3мес", callback_data=CB_STATS_3MO),
                InlineKeyboardButton(text="6мес", callback_data=CB_STATS_6MO),
                InlineKeyboardButton(text="1г", callback_data=CB_STATS_1Y),
            ],
            [
                InlineKeyboardButton(text="Все", callback_data=CB_STATS_ALL),
                InlineKeyboardButton(text="📅 Свой период", callback_data=CB_STATS_CUSTOM),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_MENU_BACK),
            ],
        ]
    )
