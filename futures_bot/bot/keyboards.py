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

# --- generic FSM back-step ---
# All ``/newblock`` steps include a "⬅ Назад" inline button that
# returns to the previous prompt without losing already-entered
# data. The handler reads the current FSM state and dispatches to
# the right re-prompt.
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

# --- Symbol picker for /newblock ---
CB_SYMBOL_PICK = "sym:"                  # + <symbol>


# Symbols grouped by how well they fit the uniform-Fibonacci strategy.
# Tier emojis are passed through to the keyboard label so the trader
# can spot the recommended pairs at a glance without needing to read
# the strategy spec each time.
#
# Tier rationale (compressed from the planning conversation):
#   🥇 — daily range fits a 6-rung block comfortably + low effective spread
#   🥈 — workable but needs trend day or slightly wider block
#   🥉 — only on a strong move; spread/range ratio is tight
#   ⚠️  — listed for completeness only; not recommended for live trading
SYMBOL_TIERS: list[tuple[str, str]] = [
    # Tier A — the "always available" instruments for this strategy.
    ("XAUUSD", "🥇"),
    ("GBPJPY", "🥇"),
    ("EURJPY", "🥇"),
    ("GBPAUD", "🥇"),
    ("GBPCAD", "🥇"),
    ("GBPUSD", "🥇"),
    ("USDJPY", "🥇"),
    ("AUDJPY", "🥇"),
    # Tier B — workable.
    ("EURUSD", "🥈"),
    ("EURAUD", "🥈"),
    ("AUDUSD", "🥈"),
    ("USDCAD", "🥈"),
    # Tier C — caution.
    ("NZDJPY", "🥉"),
    ("CADJPY", "🥉"),
    ("CHFJPY", "🥉"),
    ("GBPNZD", "🥉"),
    ("NZDUSD", "🥉"),
    ("USDCHF", "🥉"),
    # Tier D — listed for completeness; trader should know the caveats.
    ("EURGBP", "⚠️"),
    ("EURCHF", "⚠️"),
    ("EURCAD", "⚠️"),
    ("EURNZD", "⚠️"),
    ("GBPCHF", "⚠️"),
    ("AUDCAD", "⚠️"),
    ("AUDCHF", "⚠️"),
    ("AUDNZD", "⚠️"),
    ("NZDCAD", "⚠️"),
    ("NZDCHF", "⚠️"),
    ("CADCHF", "⚠️"),
]


def side_keyboard() -> InlineKeyboardMarkup:
    """BUY / SELL picker with a back row.

    The back arrow returns to the symbol-picker step in /newblock;
    when reused elsewhere it just dismisses the FSM.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 BUY (лонг)", callback_data=CB_SIDE_BUY),
                InlineKeyboardButton(text="🔴 SELL (шорт)", callback_data=CB_SIDE_SELL),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_FSM_BACK)],
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    """Final plan confirmation: ✅ submit / ⬅ revise / ❌ abort."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=CB_CONFIRM),
                InlineKeyboardButton(text="❌ Отмена", callback_data=CB_CANCEL),
            ],
            [InlineKeyboardButton(text="⬅️ Назад (изменить риск)", callback_data=CB_FSM_BACK)],
        ]
    )


def back_only_keyboard() -> InlineKeyboardMarkup:
    """Single ⬅ Назад button for text-input FSM steps.

    Used on the ``zero_price`` / ``hundred_price`` / ``base_risk``
    prompts so the trader can rewind one step without typing a value
    or resorting to /menu.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_FSM_BACK)],
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


def symbol_picker_keyboard(*, columns: int = 3) -> InlineKeyboardMarkup:
    """Inline keyboard with every supported symbol, tier-prefixed.

    The strategy works with 29 instruments; rendering them as buttons
    saves the trader from typing each name (and from typo errors).
    Buttons are flowed left-to-right in ``columns`` per row — three
    fits comfortably on a phone for the 6-character names. The order
    is fixed in :data:`SYMBOL_TIERS` so muscle memory holds across
    sessions.

    Trailing row is a ❌ button that aborts the FSM entirely — useful
    when the trader changed their mind mid-flow without committing
    to a symbol yet.
    """
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for symbol, tier in SYMBOL_TIERS:
        row.append(
            InlineKeyboardButton(
                text=f"{tier} {symbol}",
                callback_data=f"{CB_SYMBOL_PICK}{symbol}",
            )
        )
        if len(row) == columns:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Bottom row: abort the FSM. We use CB_CANCEL here (not CB_FSM_BACK)
    # because step 1 has no previous step to go back to.
    rows.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data=CB_CANCEL),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)
