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
)
from futures_bot.bot.keyboards import (
    CB_BLOCK_CANCEL,
    CB_BLOCK_CREATE,
    CB_BLOCK_LIST,
    CB_CANCEL,
    CB_CANCEL_BLOCK_CONFIRM,
    CB_CANCEL_BLOCK_PICK,
    CB_CONFIRM,
    CB_MENU_BACK,
    CB_MENU_BALANCE,
    CB_MENU_BLOCK,
    CB_MENU_STATS,
    CB_SIDE_BUY,
    CB_SIDE_SELL,
    block_submenu_keyboard,
    cancel_block_confirm_keyboard,
    cancel_block_picker_keyboard,
    confirm_keyboard,
    main_menu_keyboard,
    side_keyboard,
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
    """Six-step plan builder: symbol → side → 0% → 100% → risk → cancel.

    Confirm/abort happen via inline keyboard so /cancel can keep its
    meaning as "close a block by id" rather than "abort the current
    FSM". To bail out mid-FSM the trader presses the ❌ Отмена button
    on the confirm screen or types /menu (which clears state).
    """
    await state.clear()
    await state.set_state(NewBlockFSM.SYMBOL)
    await message.answer(
        "Шаг 1/6 — отправьте символ "
        "(например, <code>XAUUSD</code>, <code>EURUSD</code>, "
        "<code>GBPJPY</code>).",
        parse_mode=ParseMode.HTML,
    )


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
        "Шаг 2/6 — выберите сторону:",
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
            "Шаг 3/6 — отправьте цену <b>0%</b> якоря.\n"
            "Для BUY это <b>верх</b> диапазона; для SELL — <b>низ</b>.",
            parse_mode=ParseMode.HTML,
        )
    await query.answer()


@router.message(NewBlockFSM.ZERO_PRICE, F.text)
async def fsm_zero_price(message: Message, state: FSMContext) -> None:
    price = _parse_float(message.text)
    if price is None or price <= 0:
        await message.answer("Отправьте одно положительное число.")
        return
    await state.update_data(zero_price=price)
    await state.set_state(NewBlockFSM.HUNDRED_PRICE)
    await message.answer(
        "Шаг 4/6 — отправьте цену <b>100%</b> якоря "
        "(противоположный конец диапазона).",
        parse_mode=ParseMode.HTML,
    )


@router.message(NewBlockFSM.HUNDRED_PRICE, F.text)
async def fsm_hundred_price(message: Message, state: FSMContext) -> None:
    price = _parse_float(message.text)
    if price is None or price <= 0:
        await message.answer("Отправьте одно положительное число.")
        return
    await state.update_data(hundred_price=price)
    await state.set_state(NewBlockFSM.BASE_RISK)
    await message.answer(
        "Шаг 5/6 — <b>базовый риск</b> в USD на 1-й ордер.\n"
        "Остальные ордера получат 1.5×, 2.25×, 3.375×, 5.06×, 7.59× "
        "от этой суммы.",
        parse_mode=ParseMode.HTML,
    )


@router.message(NewBlockFSM.BASE_RISK, F.text)
async def fsm_base_risk(message: Message, state: FSMContext) -> None:
    risk = _parse_float(message.text)
    if risk is None or risk <= 0:
        await message.answer("Отправьте одно положительное число.")
        return
    await state.update_data(base_risk=risk)
    await state.set_state(NewBlockFSM.CANCEL_PRICE)
    await message.answer(
        "Шаг 6/6 — <b>цена отмены</b>.\n"
        "Если рынок достигнет её до первого срабатывания — все "
        "ордера будут отменены, блок станет INVALID.\n\n"
        "Для BUY: <i>выше</i> 0% якоря. Для SELL: <i>ниже</i>.",
        parse_mode=ParseMode.HTML,
    )


@router.message(NewBlockFSM.CANCEL_PRICE, F.text)
async def fsm_cancel_price(
    message: Message,
    state: FSMContext,
    adapter: BrokerAdapter,
) -> None:
    cancel = _parse_float(message.text)
    if cancel is None or cancel <= 0:
        await message.answer("Отправьте одно положительное число.")
        return

    data = await state.get_data()
    symbol: str = data["symbol"]
    side = BlockSide(data["side"])
    zero_price: float = data["zero_price"]
    hundred_price: float = data["hundred_price"]
    base_risk: float = data["base_risk"]

    # Pull live broker spec + spread so we can size the lot correctly
    # and validate the range against the live spread before the trader
    # confirms.
    try:
        info = await adapter.get_symbol_info(symbol)
        tick = await adapter.get_tick(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.exception("symbol/tick fetch failed")
        await _reply_plain(message, f"❌ Не удалось получить данные по {symbol}: {exc}")
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

    # Build the plan — this also validates anchor orientation and
    # cancel-price side relative to the ladder.
    try:
        plan = build_plan(
            symbol=symbol,
            side=side,
            zero_price=zero_price,
            hundred_price=hundred_price,
            base_risk_usd=base_risk,
            cancel_price=cancel,
            symbol_spec=symbol_spec,
            lot_rounding="up",
        )
    except ValueError as exc:
        await _reply_plain(message, f"❌ План отклонён: {exc}")
        await state.clear()
        return

    # Stash the plan so the confirm step can recover it cheaply. We
    # serialise just enough to reconstruct it; the full dataclass
    # would not survive FSM JSON storage.
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
