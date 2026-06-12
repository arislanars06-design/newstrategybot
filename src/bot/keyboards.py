"""Reusable inline keyboards.

The menu structure mirrors the spec the trader laid out:

* Block (submenu) → Create / Active blocks / Cancel / Modify / Back
* Statistika (submenu) → Today / 7d / 30d / 3mo / 6mo / 1y / All / Back
* Balans
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# --- side / confirm callbacks (used by /newblock and /track FSMs) ---
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

# --- stats time-range callbacks (days; "0" means all-time) ---
CB_STATS_TODAY = "stats:1"
CB_STATS_7D = "stats:7"
CB_STATS_30D = "stats:30"
CB_STATS_3MO = "stats:90"
CB_STATS_6MO = "stats:180"
CB_STATS_1Y = "stats:365"
CB_STATS_ALL = "stats:0"


def side_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 BUY (long)", callback_data=CB_SIDE_BUY),
                InlineKeyboardButton(text="🔴 SELL (short)", callback_data=CB_SIDE_SELL),
            ]
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm", callback_data=CB_CONFIRM),
                InlineKeyboardButton(text="❌ Cancel", callback_data=CB_CANCEL),
            ]
        ]
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Top-level menu shown by /menu — Block / Statistika / Balans."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Block", callback_data=CB_MENU_BLOCK)],
            [InlineKeyboardButton(text="📊 Statistika", callback_data=CB_MENU_STATS)],
            [InlineKeyboardButton(text="💰 Balans", callback_data=CB_MENU_BALANCE)],
        ]
    )


def block_submenu_keyboard() -> InlineKeyboardMarkup:
    """Block submenu — yaratish / aktiv bloklar / bekor qilish / o'zgartirish."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Yaratish", callback_data=CB_BLOCK_CREATE)],
            [InlineKeyboardButton(text="📋 Aktiv bloklar", callback_data=CB_BLOCK_LIST)],
            [InlineKeyboardButton(text="✋ Bekor qilish", callback_data=CB_BLOCK_CANCEL)],
            [InlineKeyboardButton(text="✏️ O'zgartirish", callback_data=CB_BLOCK_MODIFY)],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=CB_MENU_BACK)],
        ]
    )


def stats_window_keyboard() -> InlineKeyboardMarkup:
    """Time-window picker for Statistika — covers daily through yearly + All."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Today", callback_data=CB_STATS_TODAY),
                InlineKeyboardButton(text="7d", callback_data=CB_STATS_7D),
                InlineKeyboardButton(text="30d", callback_data=CB_STATS_30D),
            ],
            [
                InlineKeyboardButton(text="3mo", callback_data=CB_STATS_3MO),
                InlineKeyboardButton(text="6mo", callback_data=CB_STATS_6MO),
                InlineKeyboardButton(text="1y", callback_data=CB_STATS_1Y),
            ],
            [
                InlineKeyboardButton(text="All", callback_data=CB_STATS_ALL),
                InlineKeyboardButton(text="⬅️ Orqaga", callback_data=CB_MENU_BACK),
            ],
        ]
    )
