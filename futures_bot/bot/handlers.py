"""Command and FSM handlers for the futures Telegram bot.

The Router defined here is registered with the dispatcher in
``futures_bot.bot.setup``. Handlers receive the BlockEngine and
BrokerAdapter via ``Dispatcher.workflow_data`` (see ``setup.build_dispatcher``).

The FSM design is intentionally short — six prompts + confirm —
because the uniform-Fibonacci strategy is rigid by design: anchors
plus a base risk are enough to produce a fully-sized plan.
"""

from __future__ import annotations

from html import escape as _h

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger

from futures_bot.adapters.base import BrokerAdapter
from futures_bot.bot.formatters import (
    format_balance,
    format_block_detail,
    format_block_summary,
    format_plan_preview,
)
from futures_bot.bot.keyboards import (
    CB_BLOCK_CANCEL,
    CB_BLOCK_CREATE,
    CB_BLOCK_LIST,
    CB_CANCEL,
    CB_CANCEL_BLOCK_CONFIRM,
    CB_CANCEL_BLOCK_PICK,
    CB_CONFIRM,
    CB_FSM_BACK,
    CB_MENU_BACK,
    CB_MENU_BALANCE,
    CB_MENU_BLOCK,
    CB_MENU_STATS,
    CB_SIDE_BUY,
    CB_SIDE_SELL,
    CB_SYMBOL_PICK,
    SYMBOL_TIERS,
    back_only_keyboard,
    block_submenu_keyboard,
    cancel_block_confirm_keyboard,
    cancel_block_picker_keyboard,
    confirm_keyboard,
    main_menu_keyboard,
    side_keyboard,
    symbol_picker_keyboard,
)
from futures_bot.bot.states import NewBlockFSM
from futures_bot.core.engine import BlockEngine
from futures_bot.db import (
    BlockSide,
    repository,
    session_scope,
)
from futures_bot.strategy.plan import build_plan
from futures_bot.strategy.risk import SymbolSpec

router = Router(name="futures_bot")


# ===========================================================================
# Helpers
# ===========================================================================

async def _reply_html(message: Message, text: str) -> None:
    await message.answer(text, parse_mode=ParseMode.HTML)


async def _reply_plain(message: Message, text: str) -> None:
    """Send text without HTML parsing — safer for user-supplied content."""
    await message.answer(text, parse_mode=None)


def _parse_float(text: str | None) -> float | None:
    """Best-effort float parser tolerating comma decimals and whitespace."""
    if text is None:
        return None
    cleaned = text.strip().replace(",", ".").replace(" ", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


# ===========================================================================
# /start, /help, /menu
# ===========================================================================

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Drop the trader straight into the main menu — no welcome wall."""
    await message.answer(
        "📈 <b>Futures Trading Bot</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Откройте главное меню через /menu.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer(
        "<b>Главное меню</b> — выберите действие:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == CB_MENU_BACK)
async def menu_back(query: CallbackQuery) -> None:
    await query.answer()
    if query.message is not None:
        await query.message.answer(
            "<b>Главное меню</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )


# ===========================================================================
# Main menu — Блок submenu
# ===========================================================================

@router.callback_query(F.data == CB_MENU_BLOCK)
async def menu_block(query: CallbackQuery) -> None:
    await query.answer()
    if query.message is not None:
        await query.message.answer(
            "<b>Блок</b> — выберите действие:",
            parse_mode=ParseMode.HTML,
            reply_markup=block_submenu_keyboard(),
        )


@router.callback_query(F.data == CB_BLOCK_CREATE)
async def block_create(query: CallbackQuery, state: FSMContext) -> None:
    """Inline 'Создать' button → kick off /newblock FSM."""
    await query.answer()
    if query.message is not None:
        await cmd_newblock(query.message, state)


@router.callback_query(F.data == CB_BLOCK_LIST)
async def block_list_cb(query: CallbackQuery) -> None:
    await query.answer()
    if query.message is not None:
        await cmd_list(query.message)


@router.callback_query(F.data == CB_BLOCK_CANCEL)
async def block_cancel_cb(query: CallbackQuery) -> None:
    """Show the interactive picker with one button per active block."""
    await query.answer()
    if query.message is None:
        return
    async with session_scope() as session:
        active = await repository.list_active_blocks(session)
    if not active:
        await query.message.answer("Нет активных блоков для отмены.")
        return
    await query.message.answer(
        "✋ <b>Выберите блок для закрытия:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_block_picker_keyboard(list(active)),
    )


@router.callback_query(F.data.startswith(CB_CANCEL_BLOCK_PICK))
async def cancel_block_pick(query: CallbackQuery) -> None:
    """Block button clicked → show a confirmation dialog."""
    await query.answer()
    if query.message is None or query.data is None:
        return
    try:
        block_id = int(query.data[len(CB_CANCEL_BLOCK_PICK):])
    except ValueError:
        return
    async with session_scope() as session:
        block = await repository.get_block(session, block_id)
    if block is None or block.is_terminal:
        await query.message.answer(
            f"Блок #{block_id} больше не активен.",
            reply_markup=block_submenu_keyboard(),
        )
        return
    text = (
        f"Закрыть блок <b>#{block.id}</b> "
        f"<code>{block.symbol}</code> <b>{block.side}</b>?\n\n"
        "Все ожидающие ордера будут отменены, открытые позиции — закрыты."
    )
    await query.message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_block_confirm_keyboard(block.id),
    )


@router.callback_query(F.data.startswith(CB_CANCEL_BLOCK_CONFIRM))
async def cancel_block_confirm(
    query: CallbackQuery, engine: BlockEngine
) -> None:
    await query.answer()
    if query.message is None or query.data is None:
        return
    try:
        block_id = int(query.data[len(CB_CANCEL_BLOCK_CONFIRM):])
    except ValueError:
        return
    try:
        await engine.cancel_block(block_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("cancel_block failed")
        await _reply_plain(query.message, f"❌ Ошибка закрытия блока: {exc}")
        return
    await query.message.answer(
        f"✋ Запрос на закрытие блока #{block_id} отправлен.",
        reply_markup=block_submenu_keyboard(),
    )


# ===========================================================================
# /list, /block, /cancel, /balance
# ===========================================================================

@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    async with session_scope() as session:
        active = await repository.list_active_blocks(session)
    if not active:
        await message.answer("Нет активных блоков.")
        return
    header = f"📋 <b>Активные блоки ({len(active)}):</b>"
    body = "\n\n".join(format_block_summary(b) for b in active)
    await _reply_html(message, f"{header}\n\n{body}")


@router.message(Command("block"))
async def cmd_block(message: Message) -> None:
    text = (message.text or "").strip()
    parts = text.split()
    if len(parts) < 2:
        await _reply_plain(message, "Использование: /block <id>")
        return
    try:
        block_id = int(parts[1])
    except ValueError:
        await message.answer("ID блока должен быть числом.")
        return
    async with session_scope() as session:
        block = await repository.get_block(session, block_id)
    if block is None:
        await message.answer(f"Блок #{block_id} не найден.")
        return
    await _reply_html(message, format_block_detail(block))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, engine: BlockEngine) -> None:
    text = (message.text or "").strip()
    parts = text.split()
    if len(parts) < 2:
        await _reply_plain(message, "Использование: /cancel <id>")
        return
    try:
        block_id = int(parts[1])
    except ValueError:
        await message.answer("ID блока должен быть числом.")
        return
    try:
        await engine.cancel_block(block_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("cancel_block failed")
        await _reply_plain(message, f"❌ {exc}")
        return
    await message.answer(f"Запрос на закрытие блока #{block_id} отправлен.")


@router.message(Command("balance"))
async def cmd_balance(message: Message, adapter: BrokerAdapter) -> None:
    try:
        balance = await adapter.get_account_balance()
        equity = await adapter.get_account_equity()
        free_margin = await adapter.get_free_margin()
    except Exception as exc:  # noqa: BLE001
        logger.exception("balance fetch failed")
        await _reply_plain(message, f"Не удалось получить баланс: {exc}")
        return
    await _reply_html(
        message,
        format_balance(balance=balance, equity=equity, free_margin=free_margin),
    )


@router.message(Command("symbols"))
async def cmd_symbols(message: Message) -> None:
    """Print the supported-symbol catalogue with tier markers.

    Renders the same ``SYMBOL_TIERS`` table the /newblock keyboard
    uses, but in a scrollable text form so the trader can refer to
    it without starting an FSM. Especially handy on phones where
    the keyboard grid takes effort to scroll through.
    """
    tier_label = {
        "🥇": "Рекомендованные",
        "🥈": "Хорошие",
        "🥉": "С оговорками",
        "⚠️": "Риск (не рекомендуется)",
    }
    grouped: dict[str, list[str]] = {}
    for symbol, tier in SYMBOL_TIERS:
        grouped.setdefault(tier, []).append(symbol)

    lines: list[str] = ["📊 <b>Доступные символы</b>", ""]
    # Iterate in the same order tiers are defined in SYMBOL_TIERS so
    # the output matches the keyboard.
    seen: set[str] = set()
    for _, tier in SYMBOL_TIERS:
        if tier in seen:
            continue
        seen.add(tier)
        header = f"{tier} <b>{_h(tier_label.get(tier, tier))}</b>"
        symbols_text = "   ".join(
            f"<code>{_h(s)}</code>" for s in grouped[tier]
        )
        lines.extend([header, symbols_text, ""])

    lines.append(
        "Используйте <code>/newblock</code> для создания блока — "
        "символы можно выбрать через инлайн-кнопки."
    )
    await _reply_html(message, "\n".join(lines))


@router.callback_query(F.data == CB_MENU_BALANCE)
async def menu_balance(query: CallbackQuery, adapter: BrokerAdapter) -> None:
    await query.answer()
    if query.message is not None:
        await cmd_balance(query.message, adapter)


@router.callback_query(F.data == CB_MENU_STATS)
async def menu_stats(query: CallbackQuery) -> None:
    """Statistics submenu is a follow-up feature — placeholder reply."""
    await query.answer()
    if query.message is not None:
        await query.message.answer(
            "📊 Статистика будет добавлена позже — "
            "сначала проверим работу /newblock и /list."
        )


# ===========================================================================
# /newblock — FSM
# ===========================================================================

@router.message(Command("newblock"))
async def cmd_newblock(message: Message, state: FSMContext) -> None:
    """Six-step plan builder: symbol → side → 0% → 100% → risk → confirm.

    Cancel-price is taken from the 0% anchor automatically — see
    ``NewBlockFSM`` docstring. The trader picks the symbol from an
    inline keyboard listing all 29 supported instruments, but can
    still type a custom symbol name if needed (broker-side validity
    is checked when the live tick is fetched).
    """
    await state.clear()
    await state.set_state(NewBlockFSM.SYMBOL)
    await message.answer(
        "Шаг 1/5 — выберите символ:\n\n"
        "🥇 рекомендованные · 🥈 хорошие · 🥉 с оговорками · ⚠️ риск\n\n"
        "<i>Либо отправьте название текстом, если нужного нет в списке.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=symbol_picker_keyboard(),
    )


@router.callback_query(NewBlockFSM.SYMBOL, F.data.startswith(CB_SYMBOL_PICK))
async def fsm_symbol_picked(query: CallbackQuery, state: FSMContext) -> None:
    """Inline-keyboard variant of the symbol step.

    Mirrors :func:`fsm_symbol` but reads the symbol from the callback
    data instead of message text, so a single tap advances the FSM.
    """
    if query.data is None:
        await query.answer()
        return
    symbol = query.data[len(CB_SYMBOL_PICK):]
    await state.update_data(symbol=symbol)
    await state.set_state(NewBlockFSM.SIDE)
    msg = query.message
    if msg is not None:
        await msg.answer(
            f"Символ: <code>{_h(symbol)}</code>\n\n"
            "Шаг 2/5 — выберите сторону:",
            parse_mode=ParseMode.HTML,
            reply_markup=side_keyboard(),
        )
    await query.answer()


@router.callback_query(NewBlockFSM.SYMBOL, F.data == CB_CANCEL)
async def fsm_symbol_cancel(query: CallbackQuery, state: FSMContext) -> None:
    """Abort the FSM from the symbol-picker step.

    The picker keyboard puts ❌ Отмена on the bottom row because there
    is no previous step to go back to. The handler clears state and
    drops the trader at the main menu.
    """
    await state.clear()
    if query.message is not None:
        await query.message.answer(
            "Создание блока отменено.",
            reply_markup=main_menu_keyboard(),
        )
    await query.answer()


@router.message(NewBlockFSM.SYMBOL, F.text)
async def fsm_symbol(message: Message, state: FSMContext) -> None:
    symbol = (message.text or "").strip().upper()
    # MT5 symbol names can include letters and digits, sometimes
    # a dot or underscore for Exness (e.g. "XAUUSD.s"). Be permissive.
    if len(symbol) < 4 or any(c.isspace() for c in symbol):
        await message.answer("Неверный символ. Попробуйте ещё раз.")
        return
    await state.update_data(symbol=symbol)
    await state.set_state(NewBlockFSM.SIDE)
    await message.answer(
        f"Символ: <code>{_h(symbol)}</code>\n\n"
        "Шаг 2/5 — выберите сторону:",
        parse_mode=ParseMode.HTML,
        reply_markup=side_keyboard(),
    )


@router.callback_query(NewBlockFSM.SIDE, F.data.in_({CB_SIDE_BUY, CB_SIDE_SELL}))
async def fsm_side(query: CallbackQuery, state: FSMContext) -> None:
    side = BlockSide.BUY if query.data == CB_SIDE_BUY else BlockSide.SELL
    await state.update_data(side=str(side))
    await state.set_state(NewBlockFSM.ZERO_PRICE)
    msg = query.message
    if msg is not None:
        await msg.answer(
            "Шаг 3/5 — отправьте цену <b>0%</b> якоря.\n"
            "Для BUY это <b>верх</b> диапазона; для SELL — <b>низ</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_only_keyboard(),
        )
    await query.answer()


@router.message(NewBlockFSM.ZERO_PRICE, F.text)
async def fsm_zero_price(message: Message, state: FSMContext) -> None:
    price = _parse_float(message.text)
    if price is None or price <= 0:
        await message.answer(
            "Отправьте одно положительное число.",
            reply_markup=back_only_keyboard(),
        )
        return
    await state.update_data(zero_price=price)
    await state.set_state(NewBlockFSM.HUNDRED_PRICE)
    await message.answer(
        "Шаг 4/5 — отправьте цену <b>100%</b> якоря "
        "(противоположный конец диапазона).",
        parse_mode=ParseMode.HTML,
        reply_markup=back_only_keyboard(),
    )


@router.message(NewBlockFSM.HUNDRED_PRICE, F.text)
async def fsm_hundred_price(message: Message, state: FSMContext) -> None:
    price = _parse_float(message.text)
    if price is None or price <= 0:
        await message.answer(
            "Отправьте одно положительное число.",
            reply_markup=back_only_keyboard(),
        )
        return
    await state.update_data(hundred_price=price)
    await state.set_state(NewBlockFSM.BASE_RISK)
    await message.answer(
        "Шаг 5/5 — <b>базовый риск</b> в USD на 1-й ордер.\n"
        "Остальные ордера получат 1.5×, 2.25×, 3.375×, 5.06×, 7.59× "
        "от этой суммы.\n\n"
        "<i>Цена отмены берётся автоматически из 0% якоря.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=back_only_keyboard(),
    )


@router.message(NewBlockFSM.BASE_RISK, F.text)
async def fsm_base_risk(
    message: Message,
    state: FSMContext,
    adapter: BrokerAdapter,
) -> None:
    """Final FSM step: receive base risk, build and preview the plan.

    No cancel-price prompt — the 0% anchor doubles as the
    invalidation level. For a BUY block that anchor sits above every
    entry, for a SELL block below every entry; either way it makes a
    natural cancel-on-touch barrier.
    """
    risk = _parse_float(message.text)
    if risk is None or risk <= 0:
        await message.answer(
            "Отправьте одно положительное число.",
            reply_markup=back_only_keyboard(),
        )
        return
    await state.update_data(base_risk=risk)

    data = await state.get_data()
    symbol: str = data["symbol"]
    side = BlockSide(data["side"])
    zero_price: float = data["zero_price"]
    hundred_price: float = data["hundred_price"]
    cancel_price = zero_price  # automatic — see FSM docstring

    # Pull live broker spec + spread so we can size the lot correctly
    # and validate the range against the live spread before the trader
    # confirms.
    try:
        info = await adapter.get_symbol_info(symbol)
        tick = await adapter.get_tick(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.exception("symbol/tick fetch failed")
        await _reply_plain(
            message, f"❌ Не удалось получить данные по {symbol}: {exc}"
        )
        await state.clear()
        return

    symbol_spec = SymbolSpec(
        symbol=info.symbol,
        trade_tick_size=info.trade_tick_size,
        trade_tick_value=info.trade_tick_value,
        volume_min=info.volume_min,
        volume_max=info.volume_max,
        volume_step=info.volume_step,
    )

    # Build the plan — this also validates anchor orientation. The
    # cancel-price orientation check is automatically satisfied
    # because cancel_price == zero_price, and zero_price lies on the
    # correct side of the ladder by construction.
    try:
        plan = build_plan(
            symbol=symbol,
            side=side,
            zero_price=zero_price,
            hundred_price=hundred_price,
            base_risk_usd=risk,
            cancel_price=cancel_price,
            symbol_spec=symbol_spec,
            lot_rounding="up",
        )
    except ValueError as exc:
        await _reply_plain(message, f"❌ План отклонён: {exc}")
        await state.clear()
        return

    # Stash just enough to rebuild the plan in the confirm step. The
    # full dataclass would not survive FSM JSON storage round-trip.
    await state.update_data(
        plan_payload={
            "symbol": plan.symbol,
            "side": str(plan.side),
            "zero_price": plan.zero_price,
            "hundred_price": plan.hundred_price,
            "base_risk_usd": plan.base_risk_usd,
            "cancel_price": plan.cancel_price,
            "lot_rounding": "up",
        }
    )
    await state.set_state(NewBlockFSM.CONFIRM)
    await message.answer(
        format_plan_preview(plan, typical_spread=tick.spread),
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )


@router.callback_query(NewBlockFSM.CONFIRM, F.data == CB_CANCEL)
async def fsm_cancel_plan(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if query.message is not None:
        await query.message.answer("План отменён.")
    await query.answer()


# ---------------------------------------------------------------------------
# Generic "⬅ Назад" dispatch — one handler covers every FSM step.
# ---------------------------------------------------------------------------

@router.callback_query(F.data == CB_FSM_BACK)
async def fsm_back(query: CallbackQuery, state: FSMContext) -> None:
    """Rewind the /newblock FSM by one step.

    Each step's keyboard includes a ⬅ Назад button bound to
    ``CB_FSM_BACK``. We read the current state, hop back one
    position, and resend the *previous* step's prompt — without
    clearing the FSM data, so values the trader already entered
    survive.

    The very first step (SYMBOL) has no predecessor, so its
    keyboard uses ``CB_CANCEL`` (red ❌ Отмена) instead.
    """
    current = await state.get_state()
    msg = query.message
    if msg is None:
        await query.answer()
        return

    data = await state.get_data()
    side_str = data.get("side")

    if current == NewBlockFSM.SIDE.state:
        # SIDE → SYMBOL: show the picker again.
        await state.set_state(NewBlockFSM.SYMBOL)
        await msg.answer(
            "Шаг 1/5 — выберите символ:\n\n"
            "🥇 рекомендованные · 🥈 хорошие · 🥉 с оговорками · ⚠️ риск\n\n"
            "<i>Либо отправьте название текстом, если нужного нет в списке.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=symbol_picker_keyboard(),
        )

    elif current == NewBlockFSM.ZERO_PRICE.state:
        # ZERO_PRICE → SIDE: re-show BUY/SELL.
        await state.set_state(NewBlockFSM.SIDE)
        await msg.answer(
            "Шаг 2/5 — выберите сторону:",
            reply_markup=side_keyboard(),
        )

    elif current == NewBlockFSM.HUNDRED_PRICE.state:
        # HUNDRED_PRICE → ZERO_PRICE: re-prompt for the first anchor.
        await state.set_state(NewBlockFSM.ZERO_PRICE)
        side_hint = ""
        if side_str:
            side_hint = (
                "\nДля BUY это <b>верх</b> диапазона; для SELL — <b>низ</b>."
            )
        await msg.answer(
            "Шаг 3/5 — отправьте цену <b>0%</b> якоря." + side_hint,
            parse_mode=ParseMode.HTML,
            reply_markup=back_only_keyboard(),
        )

    elif current == NewBlockFSM.BASE_RISK.state:
        # BASE_RISK → HUNDRED_PRICE.
        await state.set_state(NewBlockFSM.HUNDRED_PRICE)
        await msg.answer(
            "Шаг 4/5 — отправьте цену <b>100%</b> якоря "
            "(противоположный конец диапазона).",
            parse_mode=ParseMode.HTML,
            reply_markup=back_only_keyboard(),
        )

    elif current == NewBlockFSM.CONFIRM.state:
        # CONFIRM → BASE_RISK: trader wants to re-enter risk.
        await state.set_state(NewBlockFSM.BASE_RISK)
        await msg.answer(
            "Шаг 5/5 — <b>базовый риск</b> в USD на 1-й ордер.\n"
            "Введите новое значение, чтобы пересчитать план.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_only_keyboard(),
        )

    else:
        # No previous step (or FSM cleared) — drop to main menu.
        await state.clear()
        await msg.answer(
            "Главное меню:",
            reply_markup=main_menu_keyboard(),
        )

    await query.answer()


@router.callback_query(NewBlockFSM.CONFIRM, F.data == CB_CONFIRM)
async def fsm_confirm_plan(
    query: CallbackQuery,
    state: FSMContext,
    engine: BlockEngine,
    adapter: BrokerAdapter,
) -> None:
    """Commit the previewed plan to the broker."""
    data = await state.get_data()
    payload = data.get("plan_payload")
    msg = query.message
    if payload is None or msg is None:
        await state.clear()
        await query.answer()
        return

    # Re-fetch symbol spec to rebuild a SymbolSpec for build_plan().
    # Cheaper than serialising the dataclass through FSM storage and
    # avoids a stale-spec window if the trader took a long break
    # between preview and confirm.
    try:
        info = await adapter.get_symbol_info(payload["symbol"])
    except Exception as exc:  # noqa: BLE001
        await _reply_plain(msg, f"❌ Не удалось обновить spec символа: {exc}")
        await state.clear()
        await query.answer()
        return

    symbol_spec = SymbolSpec(
        symbol=info.symbol,
        trade_tick_size=info.trade_tick_size,
        trade_tick_value=info.trade_tick_value,
        volume_min=info.volume_min,
        volume_max=info.volume_max,
        volume_step=info.volume_step,
    )
    try:
        plan = build_plan(
            symbol=payload["symbol"],
            side=BlockSide(payload["side"]),
            zero_price=payload["zero_price"],
            hundred_price=payload["hundred_price"],
            base_risk_usd=payload["base_risk_usd"],
            cancel_price=payload["cancel_price"],
            symbol_spec=symbol_spec,
            lot_rounding=payload.get("lot_rounding", "up"),
        )
    except ValueError as exc:
        await _reply_plain(msg, f"❌ План больше не валиден: {exc}")
        await state.clear()
        await query.answer()
        return

    await msg.answer("Размещаю ордера…")
    await query.answer()

    try:
        block = await engine.create_block(plan, chat_id=msg.chat.id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("create_block failed")
        await _reply_plain(msg, f"❌ Ошибка размещения: {exc}")
        await state.clear()
        return

    await msg.answer(
        f"✅ Блок <b>#{block.id}</b> создан и активен.",
        parse_mode=ParseMode.HTML,
    )
    await state.clear()
