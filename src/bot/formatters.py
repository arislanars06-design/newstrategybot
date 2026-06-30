"""Message formatting helpers — keep handlers focused on flow logic.

Most formatters return Telegram-ready HTML. The few that contain raw
user-supplied content (block notes) escape it explicitly so the parser
never trips on accidental ``<`` / ``>`` characters.

UI strings are in Russian — the trader requested a Russian interface.
Trading terms (BUY/SELL/LONG/SHORT/TP/SL/WIN/LOSS/INVALID) are kept in
their established Latin form because that's how every Russian-speaking
crypto trader writes them on charts and in conversations.

Times are stored in UTC but rendered in the trader's local timezone
(Asia/Tashkent, UTC+5, no DST) so the chat output matches what they
see on Binance and on their watch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape as _h
from typing import Any

from src.core.notifications import Notification, NotificationType
from src.core.risk import order_risk_amount, total_block_risk
from src.db import Block, Order
from src.db.enums import BlockSide, BlockStatus, OrderState


# ---------- timezone / time helpers ----------

# Asia/Tashkent is UTC+5 year-round (no daylight saving). Hard-coding
# the offset avoids a runtime dependency on tzdata / pytz / zoneinfo
# and is correct for the trader's location.
_TASHKENT_OFFSET = timedelta(hours=5)
_TASHKENT_TZ = timezone(_TASHKENT_OFFSET, name="UTC+5")


def _to_tashkent(dt: datetime) -> datetime:
    """Convert a (possibly naive UTC) datetime to Tashkent local time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_TASHKENT_TZ)


def _format_local(dt: datetime) -> str:
    """Render a datetime as ``YYYY-MM-DD HH:MM UTC+5``."""
    local = _to_tashkent(dt)
    return f"{local:%Y-%m-%d %H:%M} UTC+5"


def _format_relative(dt: datetime, *, now: datetime | None = None) -> str:
    """Render a humanised delta vs now ("2ч 15м назад", "3д 1ч назад")."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(tz=timezone.utc)
    secs = int((now - dt).total_seconds())
    if secs < 0:
        return "в будущем"
    if secs < 60:
        return "только что"
    if secs < 3600:
        return f"{secs // 60} мин назад"
    if secs < 86_400:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}ч {m}м назад"
    d = secs // 86_400
    h = (secs % 86_400) // 3600
    return f"{d}д {h}ч назад"


# ---------- emoji mappings ----------


def _status_emoji(status: BlockStatus) -> str:
    return {
        BlockStatus.CREATED: "📝",
        BlockStatus.ACTIVE: "🟡",
        BlockStatus.WIN: "🟢",
        BlockStatus.LOSS: "🔴",
        BlockStatus.INVALID: "⚫",
        BlockStatus.ERROR: "🚨",
    }.get(status, "•")


# Short tag the user wants in the per-rung list ("1/6 SL"). The
# trader's vocabulary is slightly different from the engine's:
# TRIGGERED (the position is open and waiting on its TP/SL) reads as
# "АКТИВЕН" in their world, while a CREATED-but-not-yet-placed rung
# reads as "ЖДЁТ".
_ORDER_STATE_LABEL: dict[OrderState, str] = {
    OrderState.PENDING: "ЖДЁТ",
    OrderState.TRIGGERED: "АКТИВЕН",
    OrderState.TP_HIT: "TP",
    OrderState.SL_HIT: "SL",
    OrderState.CANCELLED: "ОТМЕНА",
    OrderState.ERROR: "ОШИБКА",
}


def _signed(amount: float, places: int = 2) -> str:
    """Render a number with an explicit +/- sign."""
    sign = "+" if amount >= 0 else ""
    return f"{sign}{round(amount, places)}"


def _block_trade_counter(block: Block) -> str:
    """One-line summary of how many rungs are in each state.

    Renders a compact "closed/total (counts)" string the trader sees
    at the top of the block detail view and on every active-block
    list entry. Closed = TP, SL, ОТМЕНА, ОШИБКА; the trade is no
    longer in play. Active = currently holding a position. Waiting =
    not yet triggered.

    Output examples:
        "0/8 (8 ЖДЁТ)"
        "3/8 (1 TP, 1 SL, 1 АКТИВЕН, 5 ЖДЁТ)"
        "8/8 (1 TP, 7 SL)"
    """
    counts: dict[str, int] = {}
    for o in block.orders:
        label = _ORDER_STATE_LABEL.get(o.state, str(o.state))
        counts[label] = counts.get(label, 0) + 1
    total = len(block.orders)
    closed_labels = {"TP", "SL", "ОТМЕНА", "ОШИБКА"}
    closed = sum(v for k, v in counts.items() if k in closed_labels)
    # Stable display order matches the lifecycle.
    order_priority = ["TP", "SL", "АКТИВЕН", "ЖДЁТ", "ОТМЕНА", "ОШИБКА"]
    parts = [f"{k} {counts[k]}" for k in order_priority if counts.get(k)]
    return f"{closed}/{total} ({', '.join(parts)})" if parts else f"0/{total}"


def _block_realised_pnl(block: Block) -> float:
    """Sum of realised PnL across the block's closed rungs (DB only)."""
    return round(sum((o.pnl or 0.0) for o in block.orders if o.pnl is not None), 4)


# =============================================================================
# Block / order rendering
# =============================================================================


def format_block_summary(block: Block) -> str:
    """Multi-line summary used in the /list response.

    Shows ID, symbol/side, status, opening time (Tashkent + relative),
    trade counter, realised PnL, and cancel price. The realised PnL
    here is computed straight from the eager-loaded ``orders`` rows
    (sum of closed rungs' ``pnl``) — we deliberately do **not** call
    the exchange for unrealised PnL on every block in /list, to keep
    the command snappy and avoid hitting Binance rate limits.
    """
    realised = _block_realised_pnl(block)
    counter = _block_trade_counter(block)
    head = (
        f"{_status_emoji(block.status)} <b>#{block.id}</b> "
        f"<code>{block.symbol}</code> <b>{block.side}</b> "
        f"<i>{block.status}</i>"
    )
    time_line = (
        f"   ⏱ <code>{_format_local(block.created_at)}</code> "
        f"({_format_relative(block.created_at)})"
    )
    counter_line = f"   📊 {counter}"
    if realised != 0.0:
        counter_line += f"  💵 <b>{_signed(realised, 4)}</b>"
    cancel_line = f"   ✋ отмена: <code>{block.cancel_price}</code>"
    return "\n".join([head, time_line, counter_line, cancel_line])


def format_block_detail(
    block: Block, *, realtime_pnl: dict[str, Any] | None = None
) -> str:
    """Detailed multi-line view for ``/block <id>``.

    Layout follows the spec the trader requested:

        🟡 БЛОК #15
        BTCUSDT BUY
        Статус: ACTIVE

        Ордера:
        1/6 SL
        2/6 АКТИВЕН
        ...

        Текущий PnL: +12.5

    ``realtime_pnl`` is the dict returned by
    :meth:`BlockEngine.compute_block_realtime_pnl`. When the block is
    in a terminal state we fall back to the persisted ``net_pnl``.
    """
    total = len(block.orders)
    lines: list[str] = [
        f"{_status_emoji(block.status)} <b>БЛОК #{block.id}</b>",
        f"<code>{block.symbol}</code> <b>{block.side}</b>",
        f"Статус: <b>{block.status}</b>",
        f"⏱ Открыт: <code>{_format_local(block.created_at)}</code> "
        f"({_format_relative(block.created_at)})",
        f"📊 Сделки: {_block_trade_counter(block)}",
    ]

    if block.note:
        lines.append(f"Заметка: <i>{_h(block.note)}</i>")

    cp_state = "активна" if block.cancel_price_active else "выкл"
    lines.append(f"Цена отмены: <code>{block.cancel_price}</code> ({cp_state})")
    if block.win_order_seq is not None:
        lines.append(f"Выигравший ордер: <b>#{block.win_order_seq}</b>")

    lines.append("")
    lines.append("<b>Ордера:</b>")
    sorted_orders = sorted(block.orders, key=lambda o: o.seq)
    for o in sorted_orders:
        label = _ORDER_STATE_LABEL.get(o.state, str(o.state))
        risk = order_risk_amount(
            side=block.side,
            entry_price=o.entry_price,
            sl_price=o.sl_price,
            qty=o.qty,
        )
        pnl_part = ""
        if o.pnl is not None:
            pnl_part = f"  pnl {_signed(o.pnl, 4)}"
        lines.append(
            f"<code>{o.seq}/{total}</code> <b>{label}</b>"
            f"  вход <code>{o.entry_price}</code>"
            f"  TP <code>{o.tp_price}</code>"
            f"  SL <code>{o.sl_price}</code>"
            f"  объём <code>{o.qty}</code>"
            f"  риск <code>{round(risk, 4)}</code>"
            f"{pnl_part}"
        )

    lines.append("")
    block_risk = total_block_risk(
        side=block.side,
        rungs=((o.entry_price, o.sl_price, o.qty) for o in sorted_orders),
    )
    lines.append(f"Макс. убыток блока: <code>{block_risk}</code>")

    if block.is_terminal and block.net_pnl is not None:
        lines.append(f"Итоговый PnL: <b>{_signed(block.net_pnl, 4)}</b>")
    elif realtime_pnl is not None:
        unr = realtime_pnl.get("unrealised")
        if unr is None:
            lines.append(
                f"Реализованный PnL: <b>{_signed(realtime_pnl['realised'], 4)}</b>"
                "  (mark price недоступна)"
            )
        else:
            lines.append(
                f"Текущий PnL: <b>{_signed(realtime_pnl['total'], 4)}</b>"
                f"  (реализованный {_signed(realtime_pnl['realised'], 4)},"
                f" нереализованный {_signed(unr, 4)})"
            )
            if realtime_pnl.get("mark_price") is not None:
                lines.append(
                    f"Mark price: <code>{realtime_pnl['mark_price']}</code>"
                    f"  открытых позиций: <b>{realtime_pnl['open_count']}</b>"
                )

    lines.append("")
    if block.closed_at is not None:
        lines.append(
            f"Закрыт: <code>{_format_local(block.closed_at)}</code> "
            f"({_format_relative(block.closed_at)})"
        )

    return "\n".join(lines)


# =============================================================================
# Plan / tracker previews
# =============================================================================


def format_plan_preview(payload: dict[str, Any]) -> str:
    """Render the plan preview during /newblock confirmation."""
    side = payload["side"]
    entries: list[float] = payload["entries"]
    tps: list[float] = payload["tps"]
    sls: list[float] = payload["sls"]
    qty: float = payload["qty"]
    cancel_price: float = payload["cancel_price"]
    symbol: str = payload["symbol"]

    side_enum = BlockSide(side) if not isinstance(side, BlockSide) else side
    block_risk = total_block_risk(
        side=side_enum,
        rungs=((e, s, qty) for e, s in zip(entries, sls, strict=True)),
    )

    lines = [
        f"📋 <b>Предпросмотр плана</b> — <code>{symbol}</code> <b>{side}</b>",
        f"Цена отмены: <code>{cancel_price}</code>",
        f"Объём на ордер: <code>{qty}</code>",
        f"Макс. убыток блока: <code>{block_risk}</code>",
        "",
        "<b>Лестница:</b>",
        "<pre>",
        f"{'#':>2} {'вход':>10} {'tp':>10} {'sl':>10} {'риск':>8}",
    ]
    for i, (e, t, s) in enumerate(zip(entries, tps, sls, strict=True), start=1):
        risk = order_risk_amount(side=side_enum, entry_price=e, sl_price=s, qty=qty)
        lines.append(f"{i:>2} {e:>10} {t:>10} {s:>10} {round(risk, 4):>8}")
    lines.append("</pre>")
    return "\n".join(lines)


def format_tracker_preview(symbol: str, side: Any, result: Any) -> str:
    """Render the proposed ladder discovered from open Binance orders."""
    rungs = result.rungs
    side_enum = BlockSide(side) if not isinstance(side, BlockSide) else side
    block_risk = total_block_risk(
        side=side_enum,
        rungs=((r.entry_price, r.sl_price, r.qty) for r in rungs),
    )

    lines = [
        f"🔍 <b>Найденная лестница</b> — <code>{symbol}</code> <b>{side}</b>",
        f"Ордеров: <b>{len(rungs)}</b>",
        f"Макс. убыток блока: <code>{block_risk}</code>",
        "<pre>",
        f"{'#':>2} {'вход':>10} {'tp':>10} {'sl':>10} {'объём':>10} {'риск':>8}",
    ]
    for r in rungs:
        risk = order_risk_amount(
            side=side_enum, entry_price=r.entry_price, sl_price=r.sl_price, qty=r.qty
        )
        lines.append(
            f"{r.seq:>2} {r.entry_price:>10} {r.tp_price:>10} "
            f"{r.sl_price:>10} {r.qty:>10} {round(risk, 4):>8}"
        )
    lines.append("</pre>")
    if result.warnings:
        lines.append("")
        for w in result.warnings:
            lines.append(f"⚠️ {w}")
    return "\n".join(lines)


# =============================================================================
# Stats / reports / balance
# =============================================================================


def _stats_window_label(stats: dict[str, Any]) -> str:
    """Render a human-readable label for the stats window.

    Three regimes:
      * Custom range  — ``since`` / ``until`` are set; render in
        Tashkent local time.
      * Rolling days  — legacy fixed buckets via ``window_days``.
      * All time      — both fall back here.
    """
    since = stats.get("since")
    until = stats.get("until")
    if since is not None or until is not None:
        s = _format_local(since) if since is not None else "—"
        u = _format_local(until) if until is not None else "—"
        return f"{s} — {u}"

    days = stats.get("window_days")
    if days is None:
        return "Всё время"
    if days == 1:
        return "Сегодня (последние 24ч)"
    return f"Последние {days} дн."


def format_stats(stats: dict[str, Any]) -> str:
    label = _stats_window_label(stats)
    return (
        f"📊 <b>Статистика</b> — {label}\n"
        f"Закрытых блоков: <b>{stats['total_closed']}</b>\n"
        f"🟢 Выигрышей (WIN): <b>{stats['wins']}</b> "
        f"({stats['win_rate_pct']}%)\n"
        f"🔴 Проигрышей (LOSS): <b>{stats['losses']}</b>\n"
        f"⚫ Отменённых (INVALID): <b>{stats['invalids']}</b>\n"
        f"🚨 Ошибок: <b>{stats['errors']}</b>\n"
        f"\nИтоговый PnL: <b>{_signed(stats['net_pnl'], 4)}</b> USDT"
    )


def format_report(rows: list[dict[str, Any]], *, days: int) -> str:
    if not rows:
        return (
            f"📈 <b>Отчёт</b> — последние {days} дн.\n\n"
            "В этом периоде нет закрытых блоков."
        )
    lines = [f"📈 <b>Отчёт</b> — последние {days} дн.", "", "<pre>"]
    lines.append(f"{'дата':<12} {'W':>3} {'L':>3} {'I':>3} {'E':>3} {'итог':>10}")
    for r in rows:
        lines.append(
            f"{r['date']:<12} "
            f"{r['wins']:>3} {r['losses']:>3} "
            f"{r['invalids']:>3} {r['errors']:>3} "
            f"{_signed(r['net_pnl'], 4):>10}"
        )
    lines.append("</pre>")
    total_pnl = round(sum(r["net_pnl"] for r in rows), 4)
    lines.append(f"\nВсего PnL: <b>{_signed(total_pnl, 4)}</b> USDT")
    return "\n".join(lines)


def format_balance(usdt: float) -> str:
    return f"💰 Баланс кошелька: <b>{usdt:.4f} USDT</b>"


# =============================================================================
# Notifier rendering (channel + chat)
# =============================================================================


def render_notification(n: Notification) -> str:
    p = n.payload
    if n.type == NotificationType.BLOCK_CREATED:
        return (
            f"📦 <b>БЛОК #{n.block_id}</b> создан\n"
            f"Символ: <code>{p.get('symbol')}</code>\n"
            f"Сторона: <b>{p.get('side')}</b>\n"
            f"Ордеров: <b>{p.get('orders')}</b>\n"
            f"Цена отмены: <code>{p.get('cancel_price')}</code>\n"
            f"Статус: <b>ACTIVE</b>"
        )
    if n.type == NotificationType.ORDER_TRIGGERED:
        return (
            f"📍 <b>БЛОК #{n.block_id}</b>\n"
            f"Ордер <b>#{p.get('seq')}</b> сработал по цене "
            f"<code>{p.get('price')}</code>\n"
            f"Позиция ОТКРЫТА (объём <code>{p.get('qty')}</code>)"
        )
    if n.type == NotificationType.SL_HIT:
        pnl = p.get("pnl") or 0.0
        return (
            f"🟥 <b>БЛОК #{n.block_id}</b>\n"
            f"Ордер <b>#{p.get('seq')}</b> SL по цене "
            f"<code>{p.get('price')}</code> "
            f"({_signed(pnl, 4)})"
        )
    if n.type == NotificationType.BLOCK_WIN:
        net = p.get("net_pnl") or 0.0
        return (
            f"🟢 <b>БЛОК #{n.block_id} WIN</b>\n"
            f"Выигравший ордер: <b>#{p.get('win_order_seq')}</b>\n"
            f"Цена TP: <code>{p.get('tp_price')}</code>\n"
            f"Итоговый PnL: <b>{_signed(net, 4)}</b>\n"
            f"Оставшиеся ордера отменены."
        )
    if n.type == NotificationType.BLOCK_LOSS:
        net = p.get("net_pnl") or 0.0
        return (
            f"🔴 <b>БЛОК #{n.block_id} LOSS</b>\n"
            f"Все 6 ордеров остановлены по SL.\n"
            f"Итоговый PnL: <b>{_signed(net, 4)}</b>"
        )
    if n.type == NotificationType.BLOCK_INVALID:
        cp = p.get("cancel_price")
        mp = p.get("mark_price")
        extra = ""
        if cp is not None and mp is not None:
            extra = f"\nЦена отмены: <code>{cp}</code>, рынок: <code>{mp}</code>"
        return (
            f"⚫ <b>БЛОК #{n.block_id} INVALID</b>\n"
            f"Все ожидающие ордера отменены.{extra}"
        )
    if n.type == NotificationType.BLOCK_ERROR:
        return (
            f"🚨 <b>БЛОК #{n.block_id} ERROR</b>\n"
            f"Причина: {_h(str(p.get('reason')))}\n"
            f"Требуется ручная проверка."
        )
    if n.type == NotificationType.BLOCK_MANUAL_CLOSE:
        return f"✋ <b>БЛОК #{n.block_id}</b> закрыт вручную."
    if n.type == NotificationType.RUNG_LIQUIDATED:
        seq = p.get("seq")
        exit_price = p.get("exit_price")
        pnl = p.get("pnl") or 0.0
        replaced = p.get("replaced_seqs") or []
        chain_line = (
            f"\n🔗 Цепочка восстановлена: переразмещены ордера "
            f"{', '.join(f'#{s}' for s in replaced)}."
            if replaced
            else "\n⚠️ Следующих ступеней не было — блок завершён."
        )
        return (
            f"🔥 <b>БЛОК #{n.block_id} — Ордер #{seq} ЛИКВИДИРОВАН</b>\n"
            f"Цена выхода: <code>{exit_price}</code>\n"
            f"Потеря: <b>{_signed(pnl, 4)}</b>"
            f"{chain_line}"
        )
    return f"БЛОК #{n.block_id} — {n.type}: {n.payload}"



# =============================================================================
# /fib — Fibonacci plan preview
# =============================================================================


def format_fib_plan_preview(
    symbol: str,
    side: Any,
    rungs: list[Any],          # list[FibRungComputed]
    *,
    zero_price: float,
    hundred_price: float,
    cancel_price: float,
    leverage: float,
    first_risk_usd: float,
) -> str:
    """Render the per-rung Fibonacci plan with totals.

    ``rungs`` are the dataclass objects returned by
    :func:`src.core.fib.compute_fib_plan`. We type-erase to ``Any`` here
    to avoid pulling the import into the formatter module — the
    duck-typing lets the function stay independent of model layout.
    """
    side_str = str(side)
    range_size = hundred_price - zero_price

    lines = [
        f"📋 <b>Fib план</b> — <code>{symbol}</code> <b>{side_str}</b>",
        f"Диапазон: <code>{zero_price}</code> (0%) → "
        f"<code>{hundred_price}</code> (100%)  "
        f"Δ {range_size:+.2f}",
        f"1-й риск: <code>{first_risk_usd}</code> USDT  "
        f"Плечо: <code>{leverage}x</code>  "
        f"Цена отмены: <code>{cancel_price}</code>",
        "",
        "<pre>",
        f"{'#':>2} {'вход':>10} {'tp':>10} {'sl':>10} "
        f"{'объём':>10} {'риск$':>7} {'sl%':>5}",
    ]
    total_risk = 0.0
    total_margin = 0.0
    total_pos = 0.0
    for r in rungs:
        lines.append(
            f"{r.seq:>2} "
            f"{round(r.entry, 4):>10} "
            f"{round(r.tp, 4):>10} "
            f"{round(r.sl, 4):>10} "
            f"{round(r.qty, 6):>10} "
            f"{round(r.risk_usd, 2):>7} "
            f"{round(r.sl_pct, 2):>5}"
        )
        total_risk += r.risk_usd
        total_margin += r.margin
        total_pos += r.pos_size
    lines.append("</pre>")
    lines.extend([
        "",
        f"<b>Макс. убыток:</b> <code>{round(total_risk, 2)}</code> USDT  "
        "(если каждый SL сработает)",
        f"<b>Всего маржи:</b> <code>{round(total_margin, 2)}</code> USDT  "
        "(требуемое обеспечение)",
        f"<b>Объём позиции:</b> <code>{round(total_pos, 2)}</code> USDT",
    ])
    return "\n".join(lines)
