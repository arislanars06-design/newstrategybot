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

# --- FSM navigation ---
# Single ⬅️ Назад callback shared by every step that has a Back
# button: side keyboard, every text-input prompt, and the confirm
# screen. The handler reads the current FSM state and rewinds one
# step using a state → predecessor table.
CB_FSM_BACK = "fsm:back"

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

# --- stats time-range callbacks (days; "0" means all-time) ---
# Mirrors the crypto bot's window picker so the muscle memory the
# trader has built around /stats carries over 1:1.
CB_STATS_TODAY = "stats:1"
CB_STATS_7D = "stats:7"
CB_STATS_30D = "stats:30"
CB_STATS_3MO = "stats:90"
CB_STATS_6MO = "stats:180"
CB_STATS_1Y = "stats:365"
CB_STATS_ALL = "stats:0"

# --- /newblock instrument picker ---
# Symbol callback keeps the symbol literal in the data payload — at
# 64 bytes total Telegram budget that's safe even for "USDCAD.s" or
# "US500.cash"-style names. Anything longer than ~50 chars would
# need a separate id+lookup table; we'd notice via callback failures.
CB_SYM_PICK = "sym:pick:"        # + <symbol>
CB_SYM_CUSTOM = "sym:custom"     # fallback — type the symbol manually


# Mapping kept small and focused on the instruments most discretionary
# FX/metals traders actually pick. Anything not listed falls through to
# a generic chart emoji rather than failing — extending the table
# costs nothing.
_SYMBOL_EMOJI: dict[str, str] = {
    "XAUUSD": "🥇",
    "XAGUSD": "🥈",
    "EURUSD": "💶",
    "GBPUSD": "💷",
    "USDJPY": "💴",
    "USDCHF": "🇨🇭",
    "USDCAD": "🇨🇦",
    "AUDUSD": "🇦🇺",
    "NZDUSD": "🇳🇿",
    "BTCUSD": "₿",
    "ETHUSD": "Ξ",
}


def _emoji_for(symbol: str) -> str:
    """Pick a leading emoji for a symbol button.

    Two-step lookup: exact match first, then prefix match so Exness's
    suffixed variants (``XAUUSDm``, ``EURUSD.s``) inherit their parent
    pair's emoji without explicit listings.
    """
    upper = symbol.upper()
    if upper in _SYMBOL_EMOJI:
        return _SYMBOL_EMOJI[upper]
    for prefix, emoji in _SYMBOL_EMOJI.items():
        if upper.startswith(prefix):
            return emoji
    return "📊"


def side_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 BUY (лонг)", callback_data=CB_SIDE_BUY),
                InlineKeyboardButton(text="🔴 SELL (шорт)", callback_data=CB_SIDE_SELL),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_FSM_BACK),
            ],
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    """Final-step confirm.

    Three buttons: confirm commits the plan to the broker, back returns
    to the previous (cancel-price) step so the trader can adjust, and
    cancel clears the FSM entirely.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=CB_CONFIRM),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_FSM_BACK),
                InlineKeyboardButton(text="❌ Отмена", callback_data=CB_CANCEL),
            ],
        ]
    )


def back_only_keyboard() -> InlineKeyboardMarkup:
    """Tiny one-button keyboard attached to every text-input prompt.

    Lets the trader rewind one step without leaving the FSM. Picked
    over a reply keyboard because reply keyboards persist between
    messages and would mask other UI; an inline button right under
    the prompt is the more disposable surface.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_FSM_BACK),
        ]]
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


def stats_window_keyboard() -> InlineKeyboardMarkup:
    """Time-window picker for the 📊 Статистика screen.

    Layout mirrors the crypto bot: short windows on the first row,
    medium on the second, ``All`` and ``Back`` on the third. Each
    button carries its window size as a callback suffix so a single
    handler can serve them all.
    """
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
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_MENU_BACK),
            ],
        ]
    )


# Symbol → category mapping. Used by ``instrument_picker_keyboard``
# to group buttons under section headers. Unknown symbols (exotic
# CFDs, broker-specific names) fall through to the "other" bucket.
_SYMBOL_CATEGORY: dict[str, str] = {
    # Precious metals
    "XAUUSD": "metals", "XAGUSD": "metals",
    "XPDUSD": "metals", "XPTUSD": "metals",
    # Major FX (USD pairs the textbooks call "majors")
    "EURUSD": "fx_major", "GBPUSD": "fx_major", "USDJPY": "fx_major",
    "USDCHF": "fx_major", "USDCAD": "fx_major",
    "AUDUSD": "fx_major", "NZDUSD": "fx_major",
    # Cross-currency pairs (no USD leg)
    "EURJPY": "fx_cross", "GBPJPY": "fx_cross", "EURGBP": "fx_cross",
    "AUDJPY": "fx_cross", "CHFJPY": "fx_cross", "EURAUD": "fx_cross",
    "EURCHF": "fx_cross", "GBPCHF": "fx_cross", "AUDCAD": "fx_cross",
    "AUDNZD": "fx_cross", "NZDJPY": "fx_cross",
    # Crypto
    "BTCUSD": "crypto", "ETHUSD": "crypto", "LTCUSD": "crypto",
    "XRPUSD": "crypto", "BCHUSD": "crypto", "DOGEUSD": "crypto",
    # Equity indices
    "US30": "indices", "US500": "indices", "NAS100": "indices",
    "SPX500": "indices", "DAX40": "indices", "UK100": "indices",
    "JPN225": "indices", "AUS200": "indices",
}

_CATEGORY_LABEL: dict[str, str] = {
    "metals":   "🥇 Металлы",
    "fx_major": "💱 Major FX",
    "fx_cross": "💱 Cross FX",
    "crypto":   "🪙 Crypto",
    "indices":  "📈 Индексы",
    "other":    "📊 Прочее",
}

# Stable display order — independent of operator's ``quick_symbols``
# order so the trader always finds gold and majors in the same place.
_CATEGORY_ORDER: list[str] = [
    "metals", "fx_major", "fx_cross", "crypto", "indices", "other",
]

# Callback string for section-header buttons. No handler subscribes
# to it; ``handlers.py`` answers with a silent ack so a stray tap
# doesn't time out at the Telegram side.
CB_SYM_HEADER = "sym:header"


def _classify(symbol: str) -> str:
    """Bucket a symbol into a category, falling through to ``other``."""
    upper = symbol.upper()
    if upper in _SYMBOL_CATEGORY:
        return _SYMBOL_CATEGORY[upper]
    # Prefix fallback so Exness suffixes (XAUUSDm, EURUSD.s, etc.)
    # inherit their parent symbol's category for free.
    for prefix, category in _SYMBOL_CATEGORY.items():
        if upper.startswith(prefix):
            return category
    return "other"


def instrument_picker_keyboard(
    symbols: "list[str]",
    *,
    columns: int = 3,
) -> InlineKeyboardMarkup:
    """Symbols grouped by category, with section headers + footer row.

    Layout (top to bottom):

    1. For each non-empty category in :data:`_CATEGORY_ORDER`, a
       section-header row (``── 💱 Major FX ──``) followed by the
       symbols in that bucket wrapped to ``columns``.
    2. A trailing ``✏️ Другой / ⬅️ Назад`` row.

    Section headers carry the :data:`CB_SYM_HEADER` callback which
    the handler module answers as a silent no-op so accidental taps
    don't spin a Telegram "loading" indicator. Buckets preserve the
    order of ``symbols``, so the operator can tune within-category
    ordering via ``quick_symbols`` in ``.env`` without touching the
    keyboard code.
    """
    # Bucket symbols by category, preserving operator-supplied order
    # within each bucket. Empty strings (a stray trailing comma in
    # .env) are dropped so they don't surface as a blank button.
    buckets: dict[str, list[str]] = {cat: [] for cat in _CATEGORY_ORDER}
    for sym in symbols:
        cleaned = sym.strip()
        if not cleaned:
            continue
        buckets[_classify(cleaned)].append(cleaned)

    rows: list[list[InlineKeyboardButton]] = []
    for category in _CATEGORY_ORDER:
        bucket = buckets[category]
        if not bucket:
            continue
        # Section header — full-width label, no real interaction.
        rows.append([
            InlineKeyboardButton(
                text=f"── {_CATEGORY_LABEL[category]} ──",
                callback_data=CB_SYM_HEADER,
            )
        ])
        # Symbol buttons wrapped to ``columns``.
        row: list[InlineKeyboardButton] = []
        for sym in bucket:
            row.append(
                InlineKeyboardButton(
                    text=f"{_emoji_for(sym)} {sym}",
                    callback_data=f"{CB_SYM_PICK}{sym}",
                )
            )
            if len(row) == columns:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

    # Trailing footer row — always present even when no symbols match.
    rows.append([
        InlineKeyboardButton(text="✏️ Другой", callback_data=CB_SYM_CUSTOM),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_MENU_BLOCK),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
