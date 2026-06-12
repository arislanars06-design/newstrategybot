"""Reusable inline keyboards."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# --- side / confirm callbacks (used by /newblock and /track FSMs) ---
CB_SIDE_BUY = "side:buy"
CB_SIDE_SELL = "side:sell"
CB_CONFIRM = "confirm:yes"
CB_CANCEL = "confirm:no"

# --- main menu callbacks ---
CB_MENU_NEWBLOCK = "menu:newblock"
CB_MENU_TRACK = "menu:track"
CB_MENU_LIST = "menu:list"
CB_MENU_STATS = "menu:stats"
CB_MENU_REPORTS = "menu:reports"
CB_MENU_BALANCE = "menu:balance"
CB_MENU_HELP = "menu:help"

# --- stats time-range callbacks ---
CB_STATS_TODAY = "stats:1"
CB_STATS_7D = "stats:7"
CB_STATS_30D = "stats:30"
CB_STATS_ALL = "stats:0"   # 0 → all time


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
    """Top-level menu shown by /menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 New Block", callback_data=CB_MENU_NEWBLOCK),
                InlineKeyboardButton(text="🔍 Track Orders", callback_data=CB_MENU_TRACK),
            ],
            [
                InlineKeyboardButton(text="📋 Active Blocks", callback_data=CB_MENU_LIST),
                InlineKeyboardButton(text="📊 Statistics", callback_data=CB_MENU_STATS),
            ],
            [
                InlineKeyboardButton(text="📈 Reports", callback_data=CB_MENU_REPORTS),
                InlineKeyboardButton(text="💰 Balance", callback_data=CB_MENU_BALANCE),
            ],
            [
                InlineKeyboardButton(text="ℹ️ Help", callback_data=CB_MENU_HELP),
            ],
        ]
    )


def stats_window_keyboard() -> InlineKeyboardMarkup:
    """Quick time-window picker shown after /stats."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Today", callback_data=CB_STATS_TODAY),
                InlineKeyboardButton(text="7d", callback_data=CB_STATS_7D),
                InlineKeyboardButton(text="30d", callback_data=CB_STATS_30D),
                InlineKeyboardButton(text="All", callback_data=CB_STATS_ALL),
            ]
        ]
    )
