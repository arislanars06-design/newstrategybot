"""Command and FSM handlers for the Telegram bot.

The single Router defined here is registered with the dispatcher in
``src.bot.setup``. Handlers receive the BlockEngine and BinanceClient via
``Dispatcher.workflow_data`` (passed in ``setup.build_dispatcher``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    format_fib_plan_preview,
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
    CB_STATS_CUSTOM,
    CB_STATS_TODAY,
    block_submenu_keyboard,
    confirm_keyboard,
    main_menu_keyboard,
    side_keyboard,
    stats_window_keyboard,
)
from src.bot.states import FibBlockFSM, NewBlockFSM, StatsRangeFSM, TrackBlockFSM
from src.core.engine import BlockEngine
from src.core.fib import compute_fib_plan
from src.core.plan import EXPECTED_ORDERS_PER_BLOCK, BlockPlan
from src.db import BlockSide, repository, session_scope
from src.exchange.client import (
    RATE_LIMIT_CODES,
    BinanceClient,
    _format_rate_limit_error,
)
from binance.exceptions import BinanceAPIException

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
    """Open the main menu directly — no welcome wall, no command list.

    The trader explicitly asked for /start to drop them straight into
    the menu so they never have to memorise commands. Power-user
    commands (/track, /list, /block, /modify, ...) still work but
    aren't advertised here.
    """
    await message.answer(
        "📦 <b>newstrategybot</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Minimal help — points at /menu and stays out of the way."""
    await message.answer(
        "Откройте главное меню через /menu.",
        reply_markup=main_menu_keyboard(),
    )


# =============================================================================
# /menu — top-level inline keyboard with nested submenus
# =============================================================================


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


# ----- Block submenu --------------------------------------------------------


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
    await query.answer()
    if query.message is not None:
        # Yaratish = the Fibonacci-driven block creation flow. /track
        # and /newblock remain available as commands for traders who
        # prefer manual placement or who want the bot to place explicit
        # prices, but the menu surface is the Fib path because that is
        # the trader's primary workflow.
        await cmd_fib(query.message, state)


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
        await query.message.answer("Нет активных блоков для отмены.")
        return
    lines = ["✋ <b>Отменить блок</b>", "", "Выберите блок и выполните команду:"]
    lines.append("<pre>")
    for b in active:
        lines.append(f"/cancel {b.id}    {b.symbol} {b.side} (статус {b.status})")
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
            "Нет блоков с активной ценой отмены (изменить можно только "
            "до того, как сработает первый ордер)."
        )
        return
    lines = [
        "✏️ <b>Изменить цену отмены</b>",
        "",
        "Выберите блок и выполните команду:",
        "<pre>",
    ]
    for b in eligible:
        lines.append(
            f"/modify {b.id} <новая_цена>    "
            f"{b.symbol} {b.side} (текущая отмена: {b.cancel_price})"
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
        "📊 <b>Статистика</b> — выберите период:",
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


# Custom-range picker (📅 Свой период) — two-step FSM prompt for the
# start and end dates, both interpreted as Tashkent local (UTC+5).
# We accept the loose forms YYYY-MM-DD and DD.MM.YYYY since traders
# in this region commonly type dates in either format.
_TASHKENT_TZ_HANDLER = timezone(timedelta(hours=5), name="UTC+5")
_DATE_INPUT_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y")


def _parse_local_date(text: str) -> datetime | None:
    """Parse a YYYY-MM-DD / DD.MM.YYYY date as start-of-day Tashkent."""
    text = text.strip()
    for fmt in _DATE_INPUT_FORMATS:
        try:
            d = datetime.strptime(text, fmt)
            return d.replace(tzinfo=_TASHKENT_TZ_HANDLER)
        except ValueError:
            continue
    return None


@router.callback_query(F.data == CB_STATS_CUSTOM)
async def stats_custom_start(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    if query.message is None:
        return
    await state.set_state(StatsRangeFSM.SINCE)
    await query.message.answer(
        "📅 <b>Свой период</b>\n\n"
        "Введите <b>начальную</b> дату в формате "
        "<code>YYYY-MM-DD</code> или <code>DD.MM.YYYY</code>\n"
        "Например: <code>2026-06-01</code> или <code>01.06.2026</code>\n\n"
        "Или /cancel — для выхода.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("cancel"), StatsRangeFSM.SINCE)
@router.message(Command("cancel"), StatsRangeFSM.UNTIL)
async def stats_custom_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Выбор периода отменён.")


@router.message(StatsRangeFSM.SINCE)
async def stats_custom_since(message: Message, state: FSMContext) -> None:
    since = _parse_local_date((message.text or "").strip())
    if since is None:
        await _reply_plain(
            message,
            "Не понял дату. Используйте YYYY-MM-DD или DD.MM.YYYY.\n"
            "Например: 2026-06-01 или 01.06.2026"
        )
        return
    # Stash as ISO string — FSM storage must be JSON-serialisable.
    await state.update_data(since_iso=since.isoformat())
    await state.set_state(StatsRangeFSM.UNTIL)
    await message.answer(
        "Введите <b>конечную</b> дату в том же формате "
        "(включительно — статистика учтёт весь этот день):",
        parse_mode=ParseMode.HTML,
    )


@router.message(StatsRangeFSM.UNTIL)
async def stats_custom_until(message: Message, state: FSMContext) -> None:
    until_start = _parse_local_date((message.text or "").strip())
    if until_start is None:
        await _reply_plain(
            message,
            "Не понял дату. Используйте YYYY-MM-DD или DD.MM.YYYY."
        )
        return

    data = await state.get_data()
    since = datetime.fromisoformat(data["since_iso"])
    # Inclusive end-of-day: 23:59:59.999999 in Tashkent local time.
    until = until_start.replace(hour=23, minute=59, second=59, microsecond=999_999)
    if until < since:
        await _reply_plain(
            message,
            f"Конечная дата ({until:%Y-%m-%d}) раньше начальной "
            f"({since:%Y-%m-%d}). Введите конечную дату ещё раз:"
        )
        return

    await state.clear()
    await _send_stats(message, since=since, until=until)


# =============================================================================
# /list, /block, /cancel, /stats, /balance
# =============================================================================


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
async def cmd_block(message: Message, engine: BlockEngine) -> None:
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
        await _reply_plain(message, "Использование: /cancel <id>")
        return
    try:
        block_id = int(parts[1])
    except ValueError:
        await message.answer("ID блока должен быть числом.")
        return
    await engine.cancel_block(block_id)
    await message.answer(f"Запрос на закрытие блока #{block_id} отправлен.")


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
            "Использование: /modify <id_блока> <новая_цена_отмены>"
        )
        return
    try:
        block_id = int(parts[1])
        new_cancel = float(parts[2])
    except ValueError:
        await _reply_plain(
            message,
            "ID блока и цена отмены должны быть числами."
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
        f"✅ Блок #{block_id}: цена отмены обновлена на {new_cancel}."
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
                    "Использование: /stats [today | 7 | 30 | all]"
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
            await _reply_plain(message, "Использование: /reports [дней 1..90]")
            return
    await _send_reports(message, days=days)


async def _send_stats(
    message: Message,
    *,
    days: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> None:
    async with session_scope() as session:
        stats = await repository.aggregate_stats(
            session, days=days, since=since, until=until
        )
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
        await _reply_plain(message, f"Не удалось получить баланс: {exc}")
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
        await _reply_plain(message, "Использование: /raw <символ>  (например, /raw BTCUSDT)")
        return
    symbol = parts[1].upper()
    try:
        orders = await client.list_open_orders(symbol)
    except BinanceAPIException as exc:
        if exc.code in RATE_LIMIT_CODES:
            await _reply_plain(message, _format_rate_limit_error(exc))
            return
        logger.exception("list_open_orders failed")
        await _reply_plain(message, f"Не удалось получить ордера для {symbol}: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("list_open_orders failed")
        await _reply_plain(message, f"Не удалось получить ордера для {symbol}: {exc}")
        return

    if not orders:
        await _reply_plain(message, f"{symbol}: 0 открытых ордеров.")
        return

    # Trim to the fields the tracker actually inspects so the message
    # stays under Telegram's 4 KB ceiling even with dozens of orders.
    lines: list[str] = [f"{symbol}: {len(orders)} открытых ордер(ов)"]
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
        "Шаг 1/7 — отправьте символ (например, <code>BTCUSDT</code>).",
        parse_mode=ParseMode.HTML,
    )


@router.message(NewBlockFSM.SYMBOL, F.text)
async def fsm_symbol(message: Message, state: FSMContext) -> None:
    symbol = (message.text or "").strip().upper()
    if not symbol.isalnum() or len(symbol) < 4:
        await message.answer("Неверный символ. Попробуйте ещё раз.")
        return
    await state.update_data(symbol=symbol)
    await state.set_state(NewBlockFSM.SIDE)
    await message.answer("Шаг 2/7 — выберите сторону:", reply_markup=side_keyboard())


@router.callback_query(NewBlockFSM.SIDE, F.data.in_({CB_SIDE_BUY, CB_SIDE_SELL}))
async def fsm_side(query: CallbackQuery, state: FSMContext) -> None:
    side = BlockSide.BUY if query.data == CB_SIDE_BUY else BlockSide.SELL
    await state.update_data(side=str(side))
    await state.set_state(NewBlockFSM.ENTRIES)
    await query.message.answer(
        f"Шаг 3/7 — отправьте <b>{EXPECTED_ORDERS_PER_BLOCK}</b> цен входа, "
        "через запятую.\nПример: <code>100,99,98,97,96,95,94,93</code>",
        parse_mode=ParseMode.HTML,
    )
    await query.answer()


@router.message(NewBlockFSM.ENTRIES, F.text)
async def fsm_entries(message: Message, state: FSMContext) -> None:
    try:
        entries = _parse_csv_floats(message.text or "")
    except ValueError:
        await message.answer("Не удалось разобрать числа. Попробуйте ещё раз.")
        return
    if len(entries) != EXPECTED_ORDERS_PER_BLOCK:
        await message.answer(
            f"Нужно ровно {EXPECTED_ORDERS_PER_BLOCK} цен входа, получено {len(entries)}."
        )
        return
    await state.update_data(entries=entries)
    await state.set_state(NewBlockFSM.TPS)
    await message.answer(
        f"Шаг 4/7 — отправьте <b>{EXPECTED_ORDERS_PER_BLOCK}</b> цен TP в том же порядке.",
        parse_mode=ParseMode.HTML,
    )


@router.message(NewBlockFSM.TPS, F.text)
async def fsm_tps(message: Message, state: FSMContext) -> None:
    try:
        tps = _parse_csv_floats(message.text or "")
    except ValueError:
        await message.answer("Не удалось разобрать числа. Попробуйте ещё раз.")
        return
    if len(tps) != EXPECTED_ORDERS_PER_BLOCK:
        await message.answer(
            f"Нужно ровно {EXPECTED_ORDERS_PER_BLOCK} цен TP, получено {len(tps)}."
        )
        return
    await state.update_data(tps=tps)
    await state.set_state(NewBlockFSM.LAST_SL)
    await message.answer(
        "Шаг 5/7 — режим цепочки: SL каждого ордера = вход следующего.\n"
        "Отправьте SL для <b>последнего</b> ордера (у него нет следующего)."
    )


@router.message(NewBlockFSM.LAST_SL, F.text)
async def fsm_last_sl(message: Message, state: FSMContext) -> None:
    try:
        last_sl = float((message.text or "").strip())
    except ValueError:
        await message.answer("Отправьте одно число.")
        return
    await state.update_data(last_sl=last_sl)
    await state.set_state(NewBlockFSM.CANCEL_PRICE)
    await message.answer("Шаг 6/7 — цена отмены (price-invalid)?")


@router.message(NewBlockFSM.CANCEL_PRICE, F.text)
async def fsm_cancel_price(message: Message, state: FSMContext) -> None:
    try:
        cancel_price = float((message.text or "").strip())
    except ValueError:
        await message.answer("Отправьте одно число.")
        return
    await state.update_data(cancel_price=cancel_price)
    await state.set_state(NewBlockFSM.QTY)
    await message.answer(
        "Шаг 7/7 — объём на каждый ордер (в базовом активе, например <code>0.01</code>)?",
        parse_mode=ParseMode.HTML,
    )


@router.message(NewBlockFSM.QTY, F.text)
async def fsm_qty(message: Message, state: FSMContext) -> None:
    try:
        qty = float((message.text or "").strip())
    except ValueError:
        await message.answer("Отправьте одно число.")
        return
    if qty <= 0:
        await message.answer("Объём должен быть положительным.")
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
    await query.message.answer("План отменён.")
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
        await _reply_plain(query.message, f"❌ План отклонён: {exc}")
        await state.clear()
        await query.answer()
        return

    await query.message.answer("Размещаю ордера…")
    await query.answer()

    try:
        block = await engine.create_block(plan, chat_id=chat_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("create_block failed")
        await _reply_plain(query.message, f"❌ Ошибка: {exc}")
        await state.clear()
        return

    await query.message.answer(
        f"✅ Блок <b>#{block.id}</b> создан и активен.",
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
        "Шаг 1/2 — отправьте символ, чьи открытые ордера хотите подхватить "
        "(например, <code>BTCUSDT</code>). Бот сам определит сторону "
        "по вашим ордерам.",
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
        await message.answer("Неверный символ. Попробуйте ещё раз.")
        return
    await state.update_data(symbol=symbol)

    # Try to auto-detect the side. If unambiguous, skip the manual
    # BUY/SELL prompt entirely.
    try:
        side, detect_err = await engine.auto_detect_track_side(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.exception("auto_detect_track_side failed")
        await _reply_plain(message, f"❌ Не удалось получить ордера: {exc}")
        await state.clear()
        return

    if side is not None:
        await message.answer(
            f"Сторона определена автоматически: <b>{side}</b> (по вашим открытым ордерам).",
            parse_mode=ParseMode.HTML,
        )
        await _run_track_discovery(message, state, engine, symbol=symbol, side=side)
        return

    # Ambiguous — fall back to manual side selection.
    await message.answer(
        f"Не удалось определить сторону автоматически: {detect_err}\n\nВыберите вручную:",
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
    await message.answer("🔍 Читаю ваши открытые ордера на Binance…")

    try:
        result = await engine.discover_tracked_orders(symbol=symbol, side=side)
    except Exception as exc:  # noqa: BLE001
        logger.exception("discover_tracked_orders failed")
        await _reply_plain(message, f"❌ Не удалось получить ордера: {exc}")
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
    await message.answer("Шаг 2/2 — цена отмены (price-invalid)?")


@router.message(TrackBlockFSM.CANCEL_PRICE, F.text)
async def track_cancel_price(message: Message, state: FSMContext) -> None:
    try:
        cancel_price = float((message.text or "").strip())
    except ValueError:
        await message.answer("Отправьте одно число.")
        return

    data = await state.get_data()
    side = BlockSide(data["side"])
    rungs = data["rungs"]
    symbol = data["symbol"]

    # Sanity-check cancel price against the rungs we just discovered.
    entries = [r["entry_price"] for r in rungs]
    if side == BlockSide.BUY and cancel_price <= max(entries):
        await message.answer(
            f"❌ Для BUY блока цена отмены должна быть выше самого высокого "
            f"входа ({max(entries)})."
        )
        return
    if side == BlockSide.SELL and cancel_price >= min(entries):
        await message.answer(
            f"❌ Для SELL блока цена отмены должна быть ниже самого низкого "
            f"входа ({min(entries)})."
        )
        return

    await state.update_data(cancel_price=cancel_price)
    await state.set_state(TrackBlockFSM.CONFIRM)
    await message.answer(
        f"📋 <b>Подтвердите отслеживание</b>\n"
        f"Символ: <code>{symbol}</code> <b>{side}</b>\n"
        f"Ордеров: <b>{len(rungs)}</b>\n"
        f"Цена отмены: <code>{cancel_price}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )


@router.callback_query(TrackBlockFSM.CONFIRM, F.data == CB_CANCEL)
async def track_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.message.answer("Отслеживание отменено.")
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
    await query.message.answer("Подхватываю ордера…")

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
        await _reply_plain(query.message, f"❌ Ошибка: {exc}")
        await state.clear()
        return

    await query.message.answer(
        f"✅ Блок <b>#{block.id}</b> подхвачен. Статус: ACTIVE",
        parse_mode=ParseMode.HTML,
    )
    await state.clear()



# =============================================================================
# /fib — bot-driven block creation from Fibonacci levels
# =============================================================================


@router.message(Command("fib"))
async def cmd_fib(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(FibBlockFSM.SYMBOL)
    await message.answer(
        "Шаг 1/6 — отправьте символ (например, <code>BTCUSDT</code>).",
        parse_mode=ParseMode.HTML,
    )


@router.message(FibBlockFSM.SYMBOL, F.text)
async def fib_symbol(message: Message, state: FSMContext) -> None:
    symbol = (message.text or "").strip().upper()
    if not symbol.isalnum() or len(symbol) < 4:
        await message.answer("Неверный символ. Попробуйте ещё раз.")
        return
    await state.update_data(symbol=symbol)
    await state.set_state(FibBlockFSM.SIDE)
    await message.answer(
        "Шаг 2/6 — выберите сторону:",
        reply_markup=side_keyboard(),
    )


@router.callback_query(FibBlockFSM.SIDE, F.data.in_({CB_SIDE_BUY, CB_SIDE_SELL}))
async def fib_side(query: CallbackQuery, state: FSMContext) -> None:
    side = BlockSide.BUY if query.data == CB_SIDE_BUY else BlockSide.SELL
    await state.update_data(side=str(side))
    await state.set_state(FibBlockFSM.ZERO_PRICE)
    if query.message is not None:
        await query.message.answer(
            "Шаг 3/6 — отправьте якорную цену <b>0%</b>.\n"
            "Для BUY блоков выберите максимум (вершина движения); "
            "лестница пойдёт вниз. Для SELL — минимум; лестница пойдёт вверх.",
            parse_mode=ParseMode.HTML,
        )
    await query.answer()


def _parse_float(text: str | None) -> float | None:
    """Lenient parser — returns None if the input isn't a positive number."""
    if not text:
        return None
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return value if value > 0 else None


@router.message(FibBlockFSM.ZERO_PRICE, F.text)
async def fib_zero_price(message: Message, state: FSMContext) -> None:
    value = _parse_float(message.text)
    if value is None:
        await message.answer("Отправьте положительное число.")
        return
    await state.update_data(zero_price=value)
    await state.set_state(FibBlockFSM.HUNDRED_PRICE)
    await message.answer("Шаг 4/6 — отправьте якорную цену <b>100%</b>.",
                         parse_mode=ParseMode.HTML)


@router.message(FibBlockFSM.HUNDRED_PRICE, F.text)
async def fib_hundred_price(message: Message, state: FSMContext) -> None:
    value = _parse_float(message.text)
    if value is None:
        await message.answer("Отправьте положительное число.")
        return
    await state.update_data(hundred_price=value)
    await state.set_state(FibBlockFSM.FIRST_RISK)
    await message.answer(
        "Шаг 5/6 — отправьте <b>риск 1-го ордера</b> в USDT "
        "(например, <code>1</code> = $1 максимальный убыток если "
        "сработает SL первого ордера).\nРиски следующих ордеров "
        "увеличиваются автоматически в 1.5×.",
        parse_mode=ParseMode.HTML,
    )


@router.message(FibBlockFSM.FIRST_RISK, F.text)
async def fib_first_risk(message: Message, state: FSMContext) -> None:
    value = _parse_float(message.text)
    if value is None:
        await message.answer("Отправьте положительное число.")
        return
    await state.update_data(first_risk=value)
    await state.set_state(FibBlockFSM.CANCEL_PRICE)
    await message.answer(
        "Шаг 6/6 — отправьте <b>цену отмены</b> (price-invalid). Если "
        "рынок достигнет её до того, как сработает любой вход, весь "
        "блок будет отменён.",
        parse_mode=ParseMode.HTML,
    )


@router.message(FibBlockFSM.CANCEL_PRICE, F.text)
async def fib_cancel_price(
    message: Message,
    state: FSMContext,
    client: BinanceClient,
) -> None:
    cancel_price = _parse_float(message.text)
    if cancel_price is None:
        await message.answer("Отправьте положительное число.")
        return

    data = await state.get_data()
    symbol: str = data["symbol"]
    side = BlockSide(data["side"])
    pos_side = "LONG" if side == BlockSide.BUY else "SHORT"

    # Pull leverage straight from Binance so the trader doesn't have
    # to repeat what they already configured on the exchange. Fall
    # back to 10x silently — get_leverage already does that.
    try:
        leverage = await client.get_leverage(symbol, pos_side)
    except BinanceAPIException as exc:
        if exc.code in RATE_LIMIT_CODES:
            await _reply_plain(message, _format_rate_limit_error(exc))
            await state.clear()
            return
        logger.exception("get_leverage failed")
        await _reply_plain(
            message, f"❌ Не удалось получить плечо для {symbol}: {exc}"
        )
        await state.clear()
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_leverage failed")
        await _reply_plain(
            message, f"❌ Не удалось получить плечо для {symbol}: {exc}"
        )
        await state.clear()
        return

    try:
        plan, rungs = compute_fib_plan(
            symbol=symbol,
            side=side,
            zero_price=data["zero_price"],
            hundred_price=data["hundred_price"],
            first_risk_usd=data["first_risk"],
            leverage=leverage,
            cancel_price=cancel_price,
        )
        plan.validate()
    except ValueError as exc:
        await _reply_plain(message, f"❌ {exc}")
        await state.clear()
        return

    await state.update_data(
        cancel_price=cancel_price,
        leverage=leverage,
    )
    await state.set_state(FibBlockFSM.CONFIRM)
    await message.answer(
        format_fib_plan_preview(
            symbol,
            side,
            rungs,
            zero_price=data["zero_price"],
            hundred_price=data["hundred_price"],
            cancel_price=cancel_price,
            leverage=leverage,
            first_risk_usd=data["first_risk"],
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_keyboard(),
    )


@router.callback_query(FibBlockFSM.CONFIRM, F.data == CB_CANCEL)
async def fib_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if query.message is not None:
        await query.message.answer("План отменён.")
    await query.answer()


@router.callback_query(FibBlockFSM.CONFIRM, F.data == CB_CONFIRM)
async def fib_confirm(
    query: CallbackQuery,
    state: FSMContext,
    engine: BlockEngine,
) -> None:
    if query.message is None:
        await query.answer()
        return
    data = await state.get_data()
    side = BlockSide(data["side"])
    chat_id = query.message.chat.id

    try:
        plan, _rungs = compute_fib_plan(
            symbol=data["symbol"],
            side=side,
            zero_price=data["zero_price"],
            hundred_price=data["hundred_price"],
            first_risk_usd=data["first_risk"],
            leverage=data["leverage"],
            cancel_price=data["cancel_price"],
        )
        plan.validate()
    except ValueError as exc:
        await _reply_plain(query.message, f"❌ План отклонён: {exc}")
        await state.clear()
        await query.answer()
        return

    await query.message.answer("Размещаю ордера на Binance…")
    await query.answer()

    try:
        block = await engine.create_block(plan, chat_id=chat_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("create_block failed (fib)")
        await _reply_plain(query.message, f"❌ Ошибка: {exc}")
        await state.clear()
        return

    await query.message.answer(
        f"✅ Блок <b>#{block.id}</b> создан и активен.",
        parse_mode=ParseMode.HTML,
    )
    await state.clear()
