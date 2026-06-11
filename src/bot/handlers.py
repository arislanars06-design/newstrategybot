"""Command and FSM handlers for the Telegram bot.

The single Router defined here is registered with the dispatcher in
``src.bot.setup``. Handlers receive the BlockEngine and BinanceClient via
``Dispatcher.workflow_data`` (passed in ``setup.build_dispatcher``).
"""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger

from src.bot.formatters import (
    format_balance,
    format_block_detail,
    format_block_summary,
    format_plan_preview,
    format_stats,
)
from src.bot.keyboards import (
    CB_CANCEL,
    CB_CONFIRM,
    CB_SIDE_BUY,
    CB_SIDE_SELL,
    confirm_keyboard,
    side_keyboard,
)
from src.bot.states import NewBlockFSM
from src.core.engine import BlockEngine
from src.core.plan import EXPECTED_ORDERS_PER_BLOCK, BlockPlan
from src.db import BlockSide, repository, session_scope
from src.exchange.client import BinanceClient

router = Router(name="newstrategybot")


# =============================================================================
# Helpers
# =============================================================================


def _parse_csv_floats(text: str) -> list[float]:
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    return [float(p) for p in parts]


async def _reply_html(message: Message, text: str) -> None:
    await message.answer(text, parse_mode=ParseMode.HTML)


# =============================================================================
# /start, /help
# =============================================================================


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await _reply_html(
        message,
        "👋 <b>newstrategybot</b>\n\n"
        "Trade blocks of 8 chained limit orders on Binance Futures.\n\n"
        "Type /help to see all commands.",
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "<b>Commands</b>\n"
        "/newblock — create a new block (interactive)\n"
        "/list — show active blocks\n"
        "/block &lt;id&gt; — block details\n"
        "/cancel &lt;id&gt; — manually close a block\n"
        "/stats — aggregate statistics\n"
        "/balance — wallet balance"
    )
    await _reply_html(message, text)


# =============================================================================
# /list, /block, /cancel, /stats, /balance
# =============================================================================


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    async with session_scope() as session:
        active = await repository.list_active_blocks(session)
    if not active:
        await message.answer("No active blocks.")
        return
    lines = [f"📋 Active blocks ({len(active)}):"]
    lines.extend(format_block_summary(b) for b in active)
    await _reply_html(message, "\n".join(lines))


@router.message(Command("block"))
async def cmd_block(message: Message) -> None:
    text = (message.text or "").strip()
    parts = text.split()
    if len(parts) < 2:
        await message.answer("Usage: /block <id>")
        return
    try:
        block_id = int(parts[1])
    except ValueError:
        await message.answer("Block id must be an integer.")
        return
    async with session_scope() as session:
        block = await repository.get_block(session, block_id)
    if block is None:
        await message.answer(f"Block #{block_id} not found.")
        return
    await _reply_html(message, format_block_detail(block))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, engine: BlockEngine) -> None:
    text = (message.text or "").strip()
    parts = text.split()
    if len(parts) < 2:
        await message.answer("Usage: /cancel <id>")
        return
    try:
        block_id = int(parts[1])
    except ValueError:
        await message.answer("Block id must be an integer.")
        return
    await engine.cancel_block(block_id)
    await message.answer(f"Requested manual close for block #{block_id}.")


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    async with session_scope() as session:
        stats = await repository.aggregate_stats(session)
    await _reply_html(message, format_stats(stats))


@router.message(Command("balance"))
async def cmd_balance(message: Message, client: BinanceClient) -> None:
    try:
        usdt = await client.get_balance_usdt()
    except Exception as exc:  # noqa: BLE001
        logger.exception("balance fetch failed")
        await message.answer(f"Failed to fetch balance: {exc}")
        return
    await _reply_html(message, format_balance(usdt))


# =============================================================================
# /newblock — FSM
# =============================================================================


@router.message(Command("newblock"))
async def cmd_newblock(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewBlockFSM.SYMBOL)
    await message.answer(
        "Step 1/7 — send the symbol (e.g. <code>BTCUSDT</code>).",
        parse_mode=ParseMode.HTML,
    )


@router.message(NewBlockFSM.SYMBOL, F.text)
async def fsm_symbol(message: Message, state: FSMContext) -> None:
    symbol = (message.text or "").strip().upper()
    if not symbol.isalnum() or len(symbol) < 4:
        await message.answer("Invalid symbol. Try again.")
        return
    await state.update_data(symbol=symbol)
    await state.set_state(NewBlockFSM.SIDE)
    await message.answer("Step 2/7 — choose side:", reply_markup=side_keyboard())


@router.callback_query(NewBlockFSM.SIDE, F.data.in_({CB_SIDE_BUY, CB_SIDE_SELL}))
async def fsm_side(query: CallbackQuery, state: FSMContext) -> None:
    side = BlockSide.BUY if query.data == CB_SIDE_BUY else BlockSide.SELL
    await state.update_data(side=str(side))
    await state.set_state(NewBlockFSM.ENTRIES)
    await query.message.answer(
        f"Step 3/7 — send <b>{EXPECTED_ORDERS_PER_BLOCK}</b> entry prices, "
        "comma-separated.\nExample: <code>100,99,98,97,96,95,94,93</code>",
        parse_mode=ParseMode.HTML,
    )
    await query.answer()


@router.message(NewBlockFSM.ENTRIES, F.text)
async def fsm_entries(message: Message, state: FSMContext) -> None:
    try:
        entries = _parse_csv_floats(message.text or "")
    except ValueError:
        await message.answer("Could not parse numbers. Try again.")
        return
    if len(entries) != EXPECTED_ORDERS_PER_BLOCK:
        await message.answer(
            f"Need exactly {EXPECTED_ORDERS_PER_BLOCK} entries, got {len(entries)}."
        )
        return
    await state.update_data(entries=entries)
    await state.set_state(NewBlockFSM.TPS)
    await message.answer(
        f"Step 4/7 — send <b>{EXPECTED_ORDERS_PER_BLOCK}</b> TP prices in the same order.",
        parse_mode=ParseMode.HTML,
    )


@router.message(NewBlockFSM.TPS, F.text)
async def fsm_tps(message: Message, state: FSMContext) -> None:
    try:
        tps = _parse_csv_floats(message.text or "")
    except ValueError:
        await message.answer("Could not parse numbers. Try again.")
        return
    if len(tps) != EXPECTED_ORDERS_PER_BLOCK:
        await message.answer(
            f"Need exactly {EXPECTED_ORDERS_PER_BLOCK} TP prices, got {len(tps)}."
        )
        return
    await state.update_data(tps=tps)
    await state.set_state(NewBlockFSM.LAST_SL)
    await message.answer(
        "Step 5/7 — chain mode: each SL = next entry.\n"
        "Send the SL for the <b>last</b> order (no successor)."
    )


@router.message(NewBlockFSM.LAST_SL, F.text)
async def fsm_last_sl(message: Message, state: FSMContext) -> None:
    try:
        last_sl = float((message.text or "").strip())
    except ValueError:
        await message.answer("Send a single number.")
        return
    await state.update_data(last_sl=last_sl)
    await state.set_state(NewBlockFSM.CANCEL_PRICE)
    await message.answer("Step 6/7 — cancel price?")


@router.message(NewBlockFSM.CANCEL_PRICE, F.text)
async def fsm_cancel_price(message: Message, state: FSMContext) -> None:
    try:
        cancel_price = float((message.text or "").strip())
    except ValueError:
        await message.answer("Send a single number.")
        return
    await state.update_data(cancel_price=cancel_price)
    await state.set_state(NewBlockFSM.QTY)
    await message.answer("Step 7/7 — quantity per rung (base asset, e.g. <code>0.01</code>)?",
                         parse_mode=ParseMode.HTML)


@router.message(NewBlockFSM.QTY, F.text)
async def fsm_qty(message: Message, state: FSMContext) -> None:
    try:
        qty = float((message.text or "").strip())
    except ValueError:
        await message.answer("Send a single number.")
        return
    if qty <= 0:
        await message.answer("Quantity must be positive.")
        return
    await state.update_data(qty=qty)

    data = await state.get_data()
    entries: list[float] = data["entries"]
    tps: list[float] = data["tps"]
    last_sl: float = data["last_sl"]
    sls = [entries[i + 1] if i + 1 < len(entries) else last_sl for i in range(len(entries))]

    preview_payload: dict[str, Any] = {
        "symbol": data["symbol"],
        "side": data["side"],
        "entries": entries,
        "tps": tps,
        "sls": sls,
        "qty": qty,
        "cancel_price": data["cancel_price"],
    }
    await state.update_data(sls=sls)
    await state.set_state(NewBlockFSM.CONFIRM)
    await message.answer(
        format_plan_preview(preview_payload),
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )


@router.callback_query(NewBlockFSM.CONFIRM, F.data == CB_CANCEL)
async def fsm_cancel_plan(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.message.answer("Plan cancelled.")
    await query.answer()


@router.callback_query(NewBlockFSM.CONFIRM, F.data == CB_CONFIRM)
async def fsm_confirm_plan(
    query: CallbackQuery,
    state: FSMContext,
    engine: BlockEngine,
) -> None:
    data = await state.get_data()
    plan = BlockPlan.with_chained_sl(
        symbol=data["symbol"],
        side=BlockSide(data["side"]),
        entries=data["entries"],
        tps=data["tps"],
        last_sl=data["last_sl"],
        qty=data["qty"],
        cancel_price=data["cancel_price"],
    )
    chat_id = query.message.chat.id

    try:
        plan.validate()
    except ValueError as exc:
        await query.message.answer(f"❌ Plan rejected: {exc}")
        await state.clear()
        await query.answer()
        return

    await query.message.answer("Placing orders…")
    await query.answer()

    try:
        block = await engine.create_block(plan, chat_id=chat_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("create_block failed")
        await query.message.answer(f"❌ Failed: {exc}")
        await state.clear()
        return

    await query.message.answer(
        f"✅ Block <b>#{block.id}</b> placed and active.",
        parse_mode=ParseMode.HTML,
    )
    await state.clear()
