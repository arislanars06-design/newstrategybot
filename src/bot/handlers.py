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
    format_report,
    format_stats,
    format_tracker_preview,
)
from src.bot.keyboards import (
    CB_BLOCK_CANCEL,
    CB_BLOCK_CREATE,
    CB_BLOCK_LIST,
    CB_BLOCK_MODIFY,
    CB_CANCEL,
    CB_CONFIRM,
    CB_MENU_BACK,
    CB_MENU_BALANCE,
    CB_MENU_BLOCK,
    CB_MENU_STATS,
    CB_SIDE_BUY,
    CB_SIDE_SELL,
    CB_STATS_1Y,
    CB_STATS_3MO,
    CB_STATS_6MO,
    CB_STATS_7D,
    CB_STATS_30D,
    CB_STATS_ALL,
    CB_STATS_TODAY,
    block_submenu_keyboard,
    confirm_keyboard,
    main_menu_keyboard,
    side_keyboard,
    stats_window_keyboard,
)
from src.bot.states import NewBlockFSM, TrackBlockFSM
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


async def _reply_plain(message: Message, text: str) -> None:
    """Send text without HTML parsing.

    Use this whenever the message contains dynamic content we don't
    fully control (an exception's repr, a tracker error, raw user text).
    Telegram's HTML parser otherwise rejects ``<`` / ``>`` characters
    that look like unsupported tags and the whole reply blows up.
    """
    await message.answer(text, parse_mode=None)


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
        "/menu — main menu (buttons)\n"
        "/newblock — create a new block (bot places orders)\n"
        "/track — adopt orders you placed manually on Binance/TV\n"
        "/list — show active blocks\n"
        "/block &lt;id&gt; — block details (with live PnL)\n"
        "/cancel &lt;id&gt; — manually close a block\n"
        "/modify &lt;id&gt; &lt;new_cancel_price&gt; — change cancel price of an active block\n"
        "/stats [days|today|all] — aggregate statistics (default: all time)\n"
        "/reports [days] — daily breakdown (default: 7 days)\n"
        "/balance — wallet balance\n"
        "/raw &lt;symbol&gt; — diagnostic: dump open orders on a symbol"
    )
    await _reply_html(message, text)


# =============================================================================
# /menu — top-level inline keyboard with nested submenus
# =============================================================================


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer(
        "<b>Main menu</b> — pick an action:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == CB_MENU_BACK)
async def menu_back(query: CallbackQuery) -> None:
    await query.answer()
    if query.message is not None:
        await query.message.answer(
            "<b>Main menu</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )


# ----- Block submenu --------------------------------------------------------


@router.callback_query(F.data == CB_MENU_BLOCK)
async def menu_block(query: CallbackQuery) -> None:
    await query.answer()
    if query.message is not None:
        await query.message.answer(
            "<b>Block</b> — pick an action:",
            parse_mode=ParseMode.HTML,
            reply_markup=block_submenu_keyboard(),
        )


@router.callback_query(F.data == CB_BLOCK_CREATE)
async def block_create(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    if query.message is not None:
        # Yaratish = adopt user-placed orders via /track. /newblock is
        # still available as a command for traders who want the bot to
        # place the orders for them, but the trader's spec uses
        # manual placement + bot tracking, so the menu surfaces /track.
        await cmd_track(query.message, state)


@router.callback_query(F.data == CB_BLOCK_LIST)
async def block_list_cb(query: CallbackQuery) -> None:
    await query.answer()
    if query.message is not None:
        await cmd_list(query.message)


@router.callback_query(F.data == CB_BLOCK_CANCEL)
async def block_cancel_cb(query: CallbackQuery) -> None:
    await query.answer()
    if query.message is None:
        return
    async with session_scope() as session:
        active = await repository.list_active_blocks(session)
    if not active:
        await query.message.answer("No active blocks to cancel.")
        return
    lines = ["✋ <b>Cancel a block</b>", "", "Pick one and run:"]
    lines.append("<pre>")
    for b in active:
        lines.append(f"/cancel {b.id}    {b.symbol} {b.side} (status {b.status})")
    lines.append("</pre>")
    await query.message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.callback_query(F.data == CB_BLOCK_MODIFY)
async def block_modify_cb(query: CallbackQuery) -> None:
    await query.answer()
    if query.message is None:
        return
    async with session_scope() as session:
        active = await repository.list_active_blocks(session)
    eligible = [
        b for b in active if b.cancel_price_active and not b.is_terminal
    ]
    if not eligible:
        await query.message.answer(
            "No blocks with an active cancel price (modifying is only "
            "allowed before any order has triggered)."
        )
        return
    lines = [
        "✏️ <b>Modify cancel price</b>",
        "",
        "Pick one and run:",
        "<pre>",
    ]
    for b in eligible:
        lines.append(
            f"/modify {b.id} <new_price>    "
            f"{b.symbol} {b.side} (current cancel: {b.cancel_price})"
        )
    lines.append("</pre>")
    await query.message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


# ----- Statistics submenu ---------------------------------------------------


@router.callback_query(F.data == CB_MENU_STATS)
async def menu_stats(query: CallbackQuery) -> None:
    await query.answer()
    if query.message is None:
        return
    await query.message.answer(
        "📊 <b>Statistika</b> — pick a window:",
        parse_mode=ParseMode.HTML,
        reply_markup=stats_window_keyboard(),
    )


@router.callback_query(F.data == CB_MENU_BALANCE)
async def menu_balance(
    query: CallbackQuery, client: BinanceClient
) -> None:
    await query.answer()
    if query.message is not None:
        await cmd_balance(query.message, client)


# Time-window quick picks under /stats menu
@router.callback_query(
    F.data.in_(
        {
            CB_STATS_TODAY,
            CB_STATS_7D,
            CB_STATS_30D,
            CB_STATS_3MO,
            CB_STATS_6MO,
            CB_STATS_1Y,
            CB_STATS_ALL,
        }
    )
)
async def stats_window(query: CallbackQuery) -> None:
    await query.answer()
    if query.message is None:
        return
    days_int = int(query.data.split(":", 1)[1])
    days = days_int if days_int > 0 else None
    await _send_stats(query.message, days=days)


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
async def cmd_block(message: Message, engine: BlockEngine) -> None:
    text = (message.text or "").strip()
    parts = text.split()
    if len(parts) < 2:
        await _reply_plain(message, "Usage: /block <id>")
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

    # Pull realtime PnL only for non-terminal blocks; terminal blocks
    # already store final net_pnl, so the formatter just uses that.
    realtime = None
    if not block.is_terminal:
        try:
            realtime = await engine.compute_block_realtime_pnl(block.id)
        except Exception:  # noqa: BLE001
            logger.exception("realtime PnL fetch failed for block={b}", b=block.id)

    await _reply_html(message, format_block_detail(block, realtime_pnl=realtime))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, engine: BlockEngine) -> None:
    text = (message.text or "").strip()
    parts = text.split()
    if len(parts) < 2:
        await _reply_plain(message, "Usage: /cancel <id>")
        return
    try:
        block_id = int(parts[1])
    except ValueError:
        await message.answer("Block id must be an integer.")
        return
    await engine.cancel_block(block_id)
    await message.answer(f"Requested manual close for block #{block_id}.")


@router.message(Command("modify"))
async def cmd_modify(message: Message, engine: BlockEngine) -> None:
    """Change the cancel price of an active block.

    Only valid while no rung has triggered yet — past that point the
    cancel-price rule no longer applies. New price must keep the same
    side relationship with the ladder (above all entries for BUY,
    below all entries for SELL).
    """
    text = (message.text or "").strip()
    parts = text.split()
    if len(parts) < 3:
        await _reply_plain(
            message,
            "Usage: /modify <block_id> <new_cancel_price>"
        )
        return
    try:
        block_id = int(parts[1])
        new_cancel = float(parts[2])
    except ValueError:
        await _reply_plain(
            message,
            "Both block_id and new_cancel_price must be numbers."
        )
        return

    try:
        await engine.modify_cancel_price(block_id, new_cancel)
    except ValueError as exc:
        await _reply_plain(message, f"❌ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("modify_cancel_price failed")
        await _reply_plain(message, f"❌ {exc}")
        return

    await message.answer(
        f"✅ Block #{block_id}: cancel price updated to {new_cancel}."
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    text = (message.text or "").strip()
    parts = text.split()
    days: int | None = None
    if len(parts) >= 2:
        token = parts[1].lower().rstrip("d")
        if token == "today":
            days = 1
        elif token == "all":
            days = None
        else:
            try:
                parsed = int(token)
            except ValueError:
                await _reply_plain(
                    message,
                    "Usage: /stats [today | 7 | 30 | all]"
                )
                return
            days = parsed if parsed > 0 else None
    await _send_stats(message, days=days)


@router.message(Command("reports"))
async def cmd_reports(message: Message) -> None:
    text = (message.text or "").strip()
    parts = text.split()
    days = 7
    if len(parts) >= 2:
        try:
            parsed = int(parts[1])
            if 1 <= parsed <= 90:
                days = parsed
        except ValueError:
            await _reply_plain(message, "Usage: /reports [days 1..90]")
            return
    await _send_reports(message, days=days)


async def _send_stats(message: Message, *, days: int | None) -> None:
    async with session_scope() as session:
        stats = await repository.aggregate_stats(session, days=days)
    await _reply_html(message, format_stats(stats))


async def _send_reports(message: Message, *, days: int) -> None:
    async with session_scope() as session:
        rows = await repository.daily_pnl_breakdown(session, days=days)
    await _reply_html(message, format_report(list(rows), days=days))


@router.message(Command("balance"))
async def cmd_balance(message: Message, client: BinanceClient) -> None:
    try:
        usdt = await client.get_balance_usdt()
    except Exception as exc:  # noqa: BLE001
        logger.exception("balance fetch failed")
        await _reply_plain(message, f"Failed to fetch balance: {exc}")
        return
    await _reply_html(message, format_balance(usdt))


@router.message(Command("raw"))
async def cmd_raw(message: Message, client: BinanceClient) -> None:
    """Diagnostic: dump every open order on a symbol as compact JSON.

    Lets the trader (or me) see exactly how Binance reports their
    manually-placed orders when /track refuses to pair them. Output
    is plain text so Telegram never tries to parse exchange payloads
    as HTML.
    """
    text = (message.text or "").strip()
    parts = text.split()
    if len(parts) < 2:
        await _reply_plain(message, "Usage: /raw <symbol>  (e.g. /raw BTCUSDT)")
        return
    symbol = parts[1].upper()
    try:
        orders = await client.list_open_orders(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.exception("list_open_orders failed")
        await _reply_plain(message, f"Failed to fetch orders for {symbol}: {exc}")
        return

    if not orders:
        await _reply_plain(message, f"{symbol}: 0 open orders.")
        return

    # Trim to the fields the tracker actually inspects so the message
    # stays under Telegram's 4 KB ceiling even with dozens of orders.
    lines: list[str] = [f"{symbol}: {len(orders)} open order(s)"]
    for o in orders:
        lines.append(
            f"  id={o.get('orderId')} type={o.get('type')} side={o.get('side')} "
            f"posSide={o.get('positionSide','BOTH')} "
            f"reduceOnly={o.get('reduceOnly')} closePos={o.get('closePosition')} "
            f"price={o.get('price')} stopPrice={o.get('stopPrice')} "
            f"qty={o.get('origQty')}"
        )
    body = "\n".join(lines)
    # Telegram caps at ~4096 chars per message; chunk if necessary.
    while body:
        await _reply_plain(message, body[:3500])
        body = body[3500:]


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
        await _reply_plain(query.message, f"❌ Plan rejected: {exc}")
        await state.clear()
        await query.answer()
        return

    await query.message.answer("Placing orders…")
    await query.answer()

    try:
        block = await engine.create_block(plan, chat_id=chat_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("create_block failed")
        await _reply_plain(query.message, f"❌ Failed: {exc}")
        await state.clear()
        return

    await query.message.answer(
        f"✅ Block <b>#{block.id}</b> placed and active.",
        parse_mode=ParseMode.HTML,
    )
    await state.clear()



# =============================================================================
# /track — adopt user-placed orders into a block
# =============================================================================


@router.message(Command("track"))
async def cmd_track(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(TrackBlockFSM.SYMBOL)
    await message.answer(
        "Step 1/2 — send the symbol whose open orders you want to adopt "
        "(e.g. <code>BTCUSDT</code>). The bot will auto-detect the side "
        "from your orders.",
        parse_mode=ParseMode.HTML,
    )


@router.message(TrackBlockFSM.SYMBOL, F.text)
async def track_symbol(
    message: Message,
    state: FSMContext,
    engine: BlockEngine,
) -> None:
    symbol = (message.text or "").strip().upper()
    if not symbol.isalnum() or len(symbol) < 4:
        await message.answer("Invalid symbol. Try again.")
        return
    await state.update_data(symbol=symbol)

    # Try to auto-detect the side. If unambiguous, skip the manual
    # BUY/SELL prompt entirely.
    try:
        side, detect_err = await engine.auto_detect_track_side(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.exception("auto_detect_track_side failed")
        await _reply_plain(message, f"❌ Could not read orders: {exc}")
        await state.clear()
        return

    if side is not None:
        await message.answer(
            f"Auto-detected side: <b>{side}</b> from your open orders.",
            parse_mode=ParseMode.HTML,
        )
        await _run_track_discovery(message, state, engine, symbol=symbol, side=side)
        return

    # Ambiguous — fall back to manual side selection.
    await message.answer(
        f"Couldn't auto-detect side: {detect_err}\n\nPick manually:",
        reply_markup=side_keyboard(),
    )
    await state.set_state(TrackBlockFSM.SIDE)


@router.callback_query(TrackBlockFSM.SIDE, F.data.in_({CB_SIDE_BUY, CB_SIDE_SELL}))
async def track_side(
    query: CallbackQuery, state: FSMContext, engine: BlockEngine
) -> None:
    side = BlockSide.BUY if query.data == CB_SIDE_BUY else BlockSide.SELL
    data = await state.get_data()
    symbol: str = data["symbol"]
    await query.answer()
    if query.message is None:
        return
    await _run_track_discovery(query.message, state, engine, symbol=symbol, side=side)


async def _run_track_discovery(
    message: Message,
    state: FSMContext,
    engine: BlockEngine,
    *,
    symbol: str,
    side: BlockSide,
) -> None:
    """Shared discovery + preview logic used by both auto and manual paths."""
    await message.answer("🔍 Reading your open orders from Binance…")

    try:
        result = await engine.discover_tracked_orders(symbol=symbol, side=side)
    except Exception as exc:  # noqa: BLE001
        logger.exception("discover_tracked_orders failed")
        await _reply_plain(message, f"❌ Could not read orders: {exc}")
        await state.clear()
        return

    if result.error:
        await _reply_plain(message, f"❌ {result.error}")
        await state.clear()
        return

    await state.update_data(
        side=str(side),
        rungs=[
            {
                "seq": r.seq,
                "entry_price": r.entry_price,
                "tp_price": r.tp_price,
                "sl_price": r.sl_price,
                "qty": r.qty,
                "entry_order_id": r.entry_order_id,
                "tp_order_id": r.tp_order_id,
                "sl_order_id": r.sl_order_id,
            }
            for r in result.rungs
        ],
        warnings=result.warnings,
    )

    await message.answer(
        format_tracker_preview(symbol, side, result),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(TrackBlockFSM.CANCEL_PRICE)
    await message.answer("Step 2/2 — cancel price?")


@router.message(TrackBlockFSM.CANCEL_PRICE, F.text)
async def track_cancel_price(message: Message, state: FSMContext) -> None:
    try:
        cancel_price = float((message.text or "").strip())
    except ValueError:
        await message.answer("Send a single number.")
        return

    data = await state.get_data()
    side = BlockSide(data["side"])
    rungs = data["rungs"]
    symbol = data["symbol"]

    # Sanity-check cancel price against the rungs we just discovered.
    entries = [r["entry_price"] for r in rungs]
    if side == BlockSide.BUY and cancel_price <= max(entries):
        await message.answer(
            f"❌ For BUY blocks the cancel price must be above the highest "
            f"entry ({max(entries)})."
        )
        return
    if side == BlockSide.SELL and cancel_price >= min(entries):
        await message.answer(
            f"❌ For SELL blocks the cancel price must be below the lowest "
            f"entry ({min(entries)})."
        )
        return

    await state.update_data(cancel_price=cancel_price)
    await state.set_state(TrackBlockFSM.CONFIRM)
    await message.answer(
        f"📋 <b>Confirm tracking</b>\n"
        f"Symbol: <code>{symbol}</code> <b>{side}</b>\n"
        f"Rungs: <b>{len(rungs)}</b>\n"
        f"Cancel price: <code>{cancel_price}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )


@router.callback_query(TrackBlockFSM.CONFIRM, F.data == CB_CANCEL)
async def track_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.message.answer("Tracking cancelled.")
    await query.answer()


@router.callback_query(TrackBlockFSM.CONFIRM, F.data == CB_CONFIRM)
async def track_confirm(
    query: CallbackQuery,
    state: FSMContext,
    engine: BlockEngine,
) -> None:
    from src.core.tracker import TrackedRung

    data = await state.get_data()
    chat_id = query.message.chat.id

    rungs = [TrackedRung(**rd) for rd in data["rungs"]]

    await query.answer()
    await query.message.answer("Adopting orders…")

    try:
        block = await engine.track_block(
            symbol=data["symbol"],
            side=BlockSide(data["side"]),
            cancel_price=data["cancel_price"],
            chat_id=chat_id,
            rungs=rungs,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("track_block failed")
        await _reply_plain(query.message, f"❌ Failed: {exc}")
        await state.clear()
        return

    await query.message.answer(
        f"✅ Block <b>#{block.id}</b> is now tracked. Status: ACTIVE",
        parse_mode=ParseMode.HTML,
    )
    await state.clear()
