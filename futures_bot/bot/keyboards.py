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
CB_BLOCK_SESSION = "block:session"     # info screen — no FSM transition

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
    """Block submenu — Создать / Активные / Сессия / Отменить / Назад.

    The session button is a pure-info shortcut: tapping it surfaces
    the current trading session(s) and the per-session recommended
    instruments, without entering any FSM. The trader can then go
    back here and tap "Создать" to use one of the recommendations.

    No /modify equivalent yet: futures-side cancel-price is set once
    and the trader prefers /cancel + new block to mid-flight tweaks.
    Easy to add later if that changes.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать", callback_data=CB_BLOCK_CREATE)],
            [InlineKeyboardButton(text="📋 Активные блоки", callback_data=CB_BLOCK_LIST)],
            [InlineKeyboardButton(text="📅 Сессия", callback_data=CB_BLOCK_SESSION)],
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


# =====================================================================
# Trading-suitability tier system
# =====================================================================
#
# Symbols are scored A–D by how well they fit the bot's Fibonacci-grid
# strategy. The score is a fixed table — it doesn't move with the
# market — but the rationale (volatility, spread economics, regime
# stability) is the same one the discretionary trader would apply
# when ranking pairs by hand:
#
# * Tier A (🥇 Рекомендованные) — the strategy's sweet spot. Good
#   intraday volatility, tight spreads relative to range, regimes
#   that respect technical levels. Gold and the JPY/GBP majors.
# * Tier B (🥈 Хорошие) — solid majors that work most days but lose
#   edge during very tight ranges. Most major pairs sit here.
# * Tier C (🥉 С оговорками) — wider spreads or thinner liquidity
#   means the strategy needs a strong setup; backtests show net
#   positive results only on directional days.
# * Tier D (⚠️ Риск) — pairs we'd avoid by default. Tight ranges,
#   pegged or semi-pegged regimes, or simply spread-to-volatility
#   ratios that eat the edge. Listed so the trader has the option
#   if they have a specific view, with a warning glyph attached.

_TIER_A: tuple[str, ...] = (
    "XAUUSD", "GBPJPY", "EURJPY", "GBPAUD",
    "GBPCAD", "GBPUSD", "USDJPY", "AUDJPY",
)
_TIER_B: tuple[str, ...] = (
    "EURUSD", "EURAUD", "AUDUSD", "USDCAD",
)
_TIER_C: tuple[str, ...] = (
    "NZDJPY", "CADJPY", "CHFJPY", "GBPNZD", "NZDUSD", "USDCHF",
)
_TIER_D: tuple[str, ...] = (
    "EURGBP", "EURCHF", "EURCAD", "EURNZD", "GBPCHF",
    "AUDCAD", "AUDCHF", "AUDNZD", "NZDCAD", "NZDCHF", "CADCHF",
)

# Reverse lookup: symbol → tier letter. The neutral "?" key denotes
# any symbol not in the table (Exness mini variants, crypto, indices,
# custom CFDs) — those still appear in the picker but with a neutral
# glyph so the trader knows there is no tier opinion attached.
_SYMBOL_TIER: dict[str, str] = {
    **{s: "A" for s in _TIER_A},
    **{s: "B" for s in _TIER_B},
    **{s: "C" for s in _TIER_C},
    **{s: "D" for s in _TIER_D},
}

_TIER_EMOJI: dict[str, str] = {
    "A": "🥇",
    "B": "🥈",
    "C": "🥉",
    "D": "⚠️",
    "?": "📊",
}

# Stable sort key — A before B before C before D before unknown.
_TIER_ORDER: dict[str, int] = {"A": 0, "B": 1, "C": 2, "D": 3, "?": 4}

# Shown as a single line in the FSM prompt before the keyboard so the
# trader knows what the emojis mean without polluting the buttons
# themselves with text labels.
INSTRUMENT_PICKER_LEGEND: str = (
    "🥇 рекомендованные · 🥈 хорошие · 🥉 с оговорками · ⚠️ риск"
)


def _tier_for(symbol: str) -> str:
    """Tier letter for ``symbol``; '?' for anything not on the table.

    Prefix fallback matches the rest of this module's behaviour:
    Exness's ``XAUUSDm`` inherits the tier of its parent ``XAUUSD``,
    so suffixed account variants don't all dump into the '?' bucket.
    """
    upper = symbol.upper()
    if upper in _SYMBOL_TIER:
        return _SYMBOL_TIER[upper]
    for prefix, tier in _SYMBOL_TIER.items():
        if upper.startswith(prefix):
            return tier
    return "?"


# Default list — the full 29-symbol tier roster, ordered exactly as
# the keyboard renders them. Operators who want a tighter picker can
# still override via ``FB_QUICK_SYMBOLS`` in ``.env``.
DEFAULT_QUICK_SYMBOLS: tuple[str, ...] = (
    _TIER_A + _TIER_B + _TIER_C + _TIER_D
)


def instrument_picker_keyboard(
    symbols: "list[str]",
    *,
    columns: int = 3,
) -> InlineKeyboardMarkup:
    """Tier-prefixed instrument picker.

    Layout invariants:

    * **Tier-ordered.** Symbols are reordered by tier letter (A → ? )
      so the strategy's recommended picks land on the top rows. Within
      a tier the operator's :data:`Settings.quick_symbols_list` order
      is preserved so they can still nudge "EURJPY first among Tier A"
      from ``.env`` without touching code.
    * **No section headers.** The legend is rendered as a single text
      line inside the FSM prompt (see ``INSTRUMENT_PICKER_LEGEND``);
      buttons carry the tier emoji as a prefix instead.
    * **Footer always present.** Even with zero symbols the trader can
      reach the custom-text path via ``✏️ Другой`` or back out via
      ``⬅️ Назад``.

    The ``columns`` parameter is kept for tests and for operators who
    want a 2-wide layout on narrow displays.
    """
    # Sort by (tier rank, original index) so unknown tier ('?') sinks
    # to the bottom while preserving operator-supplied order inside
    # each tier.
    indexed: list[tuple[int, int, str]] = []
    for idx, sym in enumerate(symbols):
        cleaned = sym.strip()
        if not cleaned:
            continue
        tier = _tier_for(cleaned)
        indexed.append((_TIER_ORDER[tier], idx, cleaned))
    indexed.sort(key=lambda t: (t[0], t[1]))

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for _tier_rank, _orig_idx, sym in indexed:
        emoji = _TIER_EMOJI[_tier_for(sym)]
        row.append(
            InlineKeyboardButton(
                text=f"{emoji} {sym}",
                callback_data=f"{CB_SYM_PICK}{sym}",
            )
        )
        if len(row) == columns:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Trailing footer — always present even when symbols are empty so
    # the trader can recover via custom text or escape to the menu.
    rows.append([
        InlineKeyboardButton(text="✏️ Другой", callback_data=CB_SYM_CUSTOM),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_MENU_BLOCK),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
