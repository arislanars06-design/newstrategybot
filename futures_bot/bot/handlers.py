"""Command and FSM handlers for the futures Telegram bot.

The Router defined here is registered with the dispatcher in
``futures_bot.bot.setup``. Handlers receive the BlockEngine and
BrokerAdapter via ``Dispatcher.workflow_data`` (see ``setup.build_dispatcher``).

The FSM design is intentionally short — six prompts + confirm —
because the uniform-Fibonacci strategy is rigid by design: anchors
plus a base risk are enough to produce a fully-sized plan.
"""

from __future__ import annotations

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
    format_stats,
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
    CB_STATS_1Y,
    CB_STATS_30D,
    CB_STATS_3MO,
    CB_STATS_6MO,
    CB_STATS_7D,
    CB_STATS_ALL,
    CB_STATS_TODAY,
    CB_SYM_CUSTOM,
    CB_SYM_PICK,
    INSTRUMENT_PICKER_LEGEND,
    back_only_keyboard,
    block_submenu_keyboard,
    cancel_block_confirm_keyboard,
    cancel_block_picker_keyboard,
    confirm_keyboard,
    instrument_picker_keyboard,
    main_menu_keyboard,
    side_keyboard,
    stats_window_keyboard,
)
from futures_bot.bot.states import NewBlockFSM
from futures_bot.config import Settings
from futures_bot.core.engine import BlockEngine
from futures_bot.db import (
    BlockSide,
    repository,
    session_scope,
)
from futures_bot.strategy.plan import build_plan, normalize_anchors
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
async def block_create(
    query: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    """Inline 'Создать' button → kick off /newblock FSM."""
    await query.answer()
    if query.message is not None:
        await cmd_newblock(query.message, state, settings)


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


@router.callback_query(F.data == CB_MENU_BALANCE)
async def menu_balance(query: CallbackQuery, adapter: BrokerAdapter) -> None:
    await query.answer()
    if query.message is not None:
        await cmd_balance(query.message, adapter)


@router.callback_query(F.data == CB_MENU_STATS)
async def menu_stats(query: CallbackQuery) -> None:
    """Open the stats window picker (Сегодня / 7д / 30д / …)."""
    await query.answer()
    if query.message is None:
        return
    await query.message.answer(
        "📊 <b>Статистика</b> — выберите период:",
        parse_mode=ParseMode.HTML,
        reply_markup=stats_window_keyboard(),
    )


# Single callback handler for every window — the chosen days count
# lives in the callback payload (e.g. ``stats:7``).
@router.callback_query(F.data.in_({
    CB_STATS_TODAY,
    CB_STATS_7D,
    CB_STATS_30D,
    CB_STATS_3MO,
    CB_STATS_6MO,
    CB_STATS_1Y,
    CB_STATS_ALL,
}))
async def stats_window(query: CallbackQuery) -> None:
    """Render stats for the chosen window."""
    await query.answer()
    if query.message is None or query.data is None:
        return
    try:
        days_int = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        return
    # ``0`` is the magic "all-time" value baked into the callback
    # strings so they fit the same ``stats:<int>`` shape.
    days = days_int if days_int > 0 else None
    async with session_scope() as session:
        stats = await repository.compute_stats(session, days=days)
    await query.message.answer(
        format_stats(stats),
        parse_mode=ParseMode.HTML,
        reply_markup=stats_window_keyboard(),
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Slash-command twin: opens the same window picker as the inline button."""
    await _reply_html(
        message,
        "📊 <b>Статистика</b> — выберите период:",
    )
    await message.answer(
        "Выберите окно ниже:",
        reply_markup=stats_window_keyboard(),
    )


# ===========================================================================
# /newblock — FSM
# ===========================================================================

@router.message(Command("newblock"))
async def cmd_newblock(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    """Six-step plan builder: symbol → side → 0% → 100% → risk → cancel.

    Step 1 surfaces an instrument picker keyboard sourced from
    ``settings.quick_symbols`` so the most common pairs are one tap
    away. The picker also offers a "Другой" button that drops the
    trader into the free-text path the existing :func:`fsm_symbol`
    handler already implements — keeping every broker-specific name
    reachable without editing the keyboard.

    Confirm/abort happen via inline keyboard so /cancel can keep its
    meaning as "close a block by id" rather than "abort the current
    FSM". To bail out mid-FSM the trader presses the ❌ Отмена button
    on the confirm screen or types /menu (which clears state).
    """
    await state.clear()
    await state.set_state(NewBlockFSM.SYMBOL)
    await message.answer(
        "Шаг 1/5 — выберите инструмент:\n\n"
        f"<i>{INSTRUMENT_PICKER_LEGEND}</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=instrument_picker_keyboard(settings.quick_symbols_list),
    )


@router.callback_query(NewBlockFSM.SYMBOL, F.data.startswith(CB_SYM_PICK))
async def fsm_symbol_pick(
    query: CallbackQuery,
    state: FSMContext,
    adapter: BrokerAdapter,
) -> None:
    """Picker button → resolve symbol against broker, advance to side."""
    if query.data is None or query.message is None:
        await query.answer()
        return
    # Picker buttons carry the operator-configured symbol verbatim;
    # don't change case here — the adapter's resolver knows about
    # broker-specific casing (e.g. Exness's lowercase 'm' suffix).
    requested = query.data[len(CB_SYM_PICK):].strip()
    if len(requested) < 3 or any(c.isspace() for c in requested):
        await query.answer("Bad symbol", show_alert=True)
        return
    await _resolve_symbol_and_advance(query.message, state, adapter, requested)
    await query.answer()


@router.callback_query(NewBlockFSM.SYMBOL, F.data == CB_SYM_CUSTOM)
async def fsm_symbol_custom(query: CallbackQuery, state: FSMContext) -> None:
    """`Другой` button → prompt for free-text symbol input.

    Stays in the SYMBOL state so the existing :func:`fsm_symbol`
    text handler picks up the next message. We only swap the prompt;
    no state change.
    """
    if query.message is not None:
        await query.message.answer(
            "Введите символ текстом — например, <code>EURJPY</code>, "
            "<code>NAS100</code>, или <code>XAUUSDm</code> для Exness mini.",
            parse_mode=ParseMode.HTML,
        )
    await query.answer()


@router.message(NewBlockFSM.SYMBOL, F.text)
async def fsm_symbol(
    message: Message,
    state: FSMContext,
    adapter: BrokerAdapter,
) -> None:
    # Preserve case: brokers are case-sensitive on suffixes
    # (``EURUSDm`` ≠ ``EURUSDM``). The adapter's resolver will try
    # both upper and as-typed; pre-uppercasing here would lose the
    # 'm'.
    requested = (message.text or "").strip()
    # MT5 symbol names can include letters, digits, dots, hash,
    # underscore. Be permissive on character set; tighten only on
    # whitespace + minimum length.
    if len(requested) < 3 or any(c.isspace() for c in requested):
        await message.answer("Неверный символ. Попробуйте ещё раз.")
        return
    await _resolve_symbol_and_advance(message, state, adapter, requested)


async def _resolve_symbol_and_advance(
    message: Message,
    state: FSMContext,
    adapter: BrokerAdapter,
    requested: str,
) -> None:
    """Resolve requested symbol against the broker, then move to SIDE.

    On failure stays in the SYMBOL state so the trader can retry
    without re-tapping `➕ Создать`. On success surfaces the
    translation (e.g. ``EURUSD`` → ``EURUSDm``) so the trader knows
    which broker symbol the rest of the block will reference.
    """
    try:
        resolved = await adapter.resolve_symbol(requested)
    except ValueError as exc:
        await _reply_plain(message, f"❌ {exc}")
        return  # stay in SYMBOL state, let user retry
    except Exception as exc:  # noqa: BLE001
        logger.exception("resolve_symbol failed for {sym}", sym=requested)
        await _reply_plain(
            message,
            f"❌ Не удалось проверить символ у брокера: {exc}",
        )
        return

    await state.update_data(symbol=resolved, requested_symbol=requested)
    await state.set_state(NewBlockFSM.SIDE)

    if resolved != requested:
        # Surface the translation so the trader knows the bot mapped
        # their input to the broker's actual name — useful debugging
        # info when something later goes wrong.
        confirmation = (
            f"✅ Выбран: <code>{resolved}</code>\n"
            f"<i>(вы указали {requested}, ваш брокер использует "
            f"{resolved})</i>"
        )
    else:
        confirmation = f"✅ Выбран: <code>{resolved}</code>"

    await message.answer(
        f"{confirmation}\n\nШаг 2/5 — выберите сторону:",
        parse_mode=ParseMode.HTML,
        reply_markup=side_keyboard(),
    )


@router.callback_query(NewBlockFSM.SIDE, F.data.in_({CB_SIDE_BUY, CB_SIDE_SELL}))
async def fsm_side(
    query: CallbackQuery,
    state: FSMContext,
    adapter: BrokerAdapter,
) -> None:
    side = BlockSide.BUY if query.data == CB_SIDE_BUY else BlockSide.SELL
    await state.update_data(side=str(side))
    await state.set_state(NewBlockFSM.ZERO_PRICE)
    msg = query.message
    if msg is not None:
        await _prompt_zero_price(msg, state, adapter)
    await query.answer()


# ---------------------------------------------------------------------
# Per-state prompt helpers (used by forward FSM and the ⬅️ Назад path)
# ---------------------------------------------------------------------
#
# Centralising the prompts here means the ⬅️ Назад handler can rewind
# state and re-render the previous step's prompt without duplicating
# the prompt text in two places. Each helper takes the target message
# to answer plus whatever DI it needs to do its job.

async def _prompt_zero_price(
    target: Message, state: FSMContext, adapter: BrokerAdapter
) -> None:
    """Step 3/6 — 0% anchor prompt with optional current-price hint."""
    data = await state.get_data()
    symbol = str(data.get("symbol") or "")
    hint = ""
    try:
        tick = await adapter.get_tick(symbol)
        hint = (
            f"\n\n📍 Текущая цена: "
            f"ask=<code>{tick.ask}</code>, "
            f"bid=<code>{tick.bid}</code>"
        )
    except Exception:  # noqa: BLE001
        logger.debug("tick fetch failed for {sym}; skipping hint", sym=symbol)

    await target.answer(
        "Шаг 3/5 — отправьте цену <b>0%</b> якоря.\n"
        "Для BUY это <b>верх</b> диапазона; для SELL — <b>низ</b>."
        + hint,
        parse_mode=ParseMode.HTML,
        reply_markup=back_only_keyboard(),
    )


async def _prompt_hundred_price(target: Message) -> None:
    """Step 4/6 — 100% anchor prompt."""
    await target.answer(
        "Шаг 4/5 — отправьте цену <b>100%</b> якоря "
        "(противоположный конец диапазона).",
        parse_mode=ParseMode.HTML,
        reply_markup=back_only_keyboard(),
    )


async def _prompt_base_risk(target: Message) -> None:
    """Step 5/5 — base risk prompt (final FSM input)."""
    await target.answer(
        "Шаг 5/5 — <b>базовый риск</b> в USD на 1-й ордер.\n"
        "Остальные ордера получат 1.5×, 2.25×, 3.375×, 5.06×, 7.59× "
        "от этой суммы.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_only_keyboard(),
    )


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
    await _prompt_hundred_price(message)


@router.message(NewBlockFSM.HUNDRED_PRICE, F.text)
async def fsm_hundred_price(message: Message, state: FSMContext) -> None:
    price = _parse_float(message.text)
    if price is None or price <= 0:
        await message.answer(
            "Отправьте одно положительное число.",
            reply_markup=back_only_keyboard(),
        )
        return

    # Auto-correct orientation: if the trader entered the anchors the
    # way they "feel" (price action chronologically) rather than the
    # way the strategy requires (BUY: 0% above; SELL: 0% below), swap
    # them and tell the trader. This catches the single most common
    # mistake — typing 2640 first instead of 2660 for a BUY on gold.
    data = await state.get_data()
    side = BlockSide(data["side"])
    zero_price = float(data["zero_price"])
    hundred_price = price

    new_zero, new_hundred, swapped = normalize_anchors(
        side, zero_price, hundred_price
    )

    await state.update_data(zero_price=new_zero, hundred_price=new_hundred)
    await state.set_state(NewBlockFSM.BASE_RISK)

    if swapped:
        side_word = "выше" if side == BlockSide.BUY else "ниже"
        await message.answer(
            f"⚠️ <i>Якоря поменяны местами автоматически: "
            f"0% = {new_zero}, 100% = {new_hundred} "
            f"(для {side} 0% должна быть {side_word}).</i>",
            parse_mode=ParseMode.HTML,
        )
    await _prompt_base_risk(message)


@router.message(NewBlockFSM.BASE_RISK, F.text)
async def fsm_base_risk(
    message: Message,
    state: FSMContext,
    adapter: BrokerAdapter,
) -> None:
    """Capture base risk and build the plan in one shot.

    Used to be the launchpad to a separate CANCEL_PRICE prompt; the
    trader explicitly opted out of that step, so we now skip it
    entirely and build the plan with ``cancel_price=None``. The
    resulting block has its cancel-price guard disabled from
    creation — :class:`BlockEngine` respects ``cancel_price_active``
    and never tries to evaluate the (placeholder) cancel price.
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
    base_risk: float = risk

    # Pull live broker spec + spread so we can size the lot correctly
    # and surface a realistic spread in the preview before the
    # trader confirms.
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

    try:
        plan = build_plan(
            symbol=symbol,
            side=side,
            zero_price=zero_price,
            hundred_price=hundred_price,
            base_risk_usd=base_risk,
            # Cancel-price guard is disabled by design — the FSM
            # no longer collects this input. The block will run
            # until fills / SL / TP / manual cancel.
            cancel_price=None,
            symbol_spec=symbol_spec,
            lot_rounding="up",
        )
    except ValueError as exc:
        await _reply_plain(message, f"❌ План отклонён: {exc}")
        await state.clear()
        return

    # Stash the plan so the confirm step can rebuild it cheaply.
    await state.update_data(
        plan_payload={
            "symbol": plan.symbol,
            "side": str(plan.side),
            "zero_price": plan.zero_price,
            "hundred_price": plan.hundred_price,
            "base_risk_usd": plan.base_risk_usd,
            "cancel_price": plan.cancel_price,    # None — placeholder
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


# ---------------------------------------------------------------------
# ⬅️ Назад navigation
# ---------------------------------------------------------------------
#
# Maps every FSM state that has a Back button to the state it should
# rewind to. ``None`` here means "no further back" (used by SYMBOL
# itself — the picker keyboard's Back goes to the block submenu via
# CB_MENU_BLOCK, handled separately).
_PREVIOUS_STATE: dict[str, str] = {
    NewBlockFSM.SIDE.state:          NewBlockFSM.SYMBOL.state,
    NewBlockFSM.ZERO_PRICE.state:    NewBlockFSM.SIDE.state,
    NewBlockFSM.HUNDRED_PRICE.state: NewBlockFSM.ZERO_PRICE.state,
    NewBlockFSM.BASE_RISK.state:     NewBlockFSM.HUNDRED_PRICE.state,
    # The CANCEL_PRICE step was removed — CONFIRM rewinds straight
    # back to BASE_RISK so the trader can adjust the risk knob.
    NewBlockFSM.CONFIRM.state:       NewBlockFSM.BASE_RISK.state,
}


@router.callback_query(F.data == CB_FSM_BACK)
async def fsm_back(
    query: CallbackQuery,
    state: FSMContext,
    adapter: BrokerAdapter,
    settings: Settings,
) -> None:
    """Rewind one FSM step and re-render the previous prompt.

    The callback is the only generic Back button in the bot — it's
    attached to every keyboard inside the /newblock FSM. We look up
    the previous state via :data:`_PREVIOUS_STATE`, transition the
    FSM, and call the same per-state prompt helpers the forward
    handlers use so the UX is identical to landing on that step the
    first time.

    Edge cases:

    * Back from SYMBOL is not handled here — the picker keyboard's
      own Назад button goes to the block submenu and is wired to
      CB_MENU_BLOCK, not CB_FSM_BACK.
    * If the FSM is in an unexpected state (e.g. user kept an old
      message around and tapped Back after the FSM was cleared), we
      silently ack and do nothing.
    """
    await query.answer()
    if query.message is None:
        return
    current = await state.get_state()
    previous = _PREVIOUS_STATE.get(current or "")
    if previous is None:
        return
    await state.set_state(previous)

    msg = query.message
    if previous == NewBlockFSM.SYMBOL.state:
        await msg.answer(
            "Шаг 1/5 — выберите инструмент:\n\n"
            f"<i>{INSTRUMENT_PICKER_LEGEND}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=instrument_picker_keyboard(settings.quick_symbols_list),
        )
    elif previous == NewBlockFSM.SIDE.state:
        await msg.answer(
            "Шаг 2/5 — выберите сторону:",
            reply_markup=side_keyboard(),
        )
    elif previous == NewBlockFSM.ZERO_PRICE.state:
        await _prompt_zero_price(msg, state, adapter)
    elif previous == NewBlockFSM.HUNDRED_PRICE.state:
        await _prompt_hundred_price(msg)
    elif previous == NewBlockFSM.BASE_RISK.state:
        await _prompt_base_risk(msg)


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
