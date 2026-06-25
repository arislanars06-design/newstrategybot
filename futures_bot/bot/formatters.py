"""Message formatting helpers — Russian UI to match the crypto bot.

Same style guide as ``src/bot/formatters.py``:

* Russian prose, Latin trading terms (BUY/SELL/TP/SL/WIN/LOSS).
* Tashkent local time (UTC+5, no DST) for timestamps.
* HTML formatting; user-supplied content is escaped with ``html.escape``.
* Returns Telegram-ready strings; never sends anything itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape as _h
from typing import Any

from futures_bot.core.notifications import Notification, NotificationType
from futures_bot.db.enums import BlockStatus, OrderState
from futures_bot.db.models import Block
from futures_bot.db.repository import StatsSummary
from futures_bot.strategy.plan import BlockPlan


# ---------- timezone ----------

_TASHKENT_OFFSET = timedelta(hours=5)
_TASHKENT_TZ = timezone(_TASHKENT_OFFSET, name="UTC+5")


def _to_tashkent(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_TASHKENT_TZ)


def _format_local(dt: datetime) -> str:
    local = _to_tashkent(dt)
    return f"{local:%Y-%m-%d %H:%M} UTC+5"


def _format_relative(dt: datetime, *, now: datetime | None = None) -> str:
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


# ---------- emojis & state labels ----------

def _status_emoji(status: BlockStatus) -> str:
    return {
        BlockStatus.CREATED: "📝",
        BlockStatus.ACTIVE: "🟡",
        BlockStatus.WIN: "🟢",
        BlockStatus.LOSS: "🔴",
        BlockStatus.INVALID: "⚫",
        BlockStatus.ERROR: "🚨",
    }.get(status, "•")


_ORDER_STATE_LABEL: dict[OrderState, str] = {
    OrderState.PENDING: "ЖДЁТ",
    OrderState.OPEN: "АКТИВЕН",
    OrderState.TP_HIT: "TP",
    OrderState.SL_HIT: "SL",
    OrderState.CANCELLED: "ОТМЕНА",
    OrderState.ERROR: "ОШИБКА",
}


def _signed(amount: float, places: int = 2) -> str:
    sign = "+" if amount >= 0 else ""
    return f"{sign}{round(amount, places)}"


def _block_trade_counter(block: Block) -> str:
    """Compact closed/total summary with per-state breakdown."""
    counts: dict[str, int] = {}
    for o in block.orders:
        label = _ORDER_STATE_LABEL.get(o.state, str(o.state))
        counts[label] = counts.get(label, 0) + 1
    total = len(block.orders)
    closed_labels = {"TP", "SL", "ОТМЕНА", "ОШИБКА"}
    closed = sum(v for k, v in counts.items() if k in closed_labels)
    order_priority = ["TP", "SL", "АКТИВЕН", "ЖДЁТ", "ОТМЕНА", "ОШИБКА"]
    parts = [f"{k} {counts[k]}" for k in order_priority if counts.get(k)]
    return f"{closed}/{total} ({', '.join(parts)})" if parts else f"0/{total}"


# ============================================================================
# Plan preview (used by /newblock confirm step)
# ============================================================================

def format_plan_preview(plan: BlockPlan, *, typical_spread: float) -> str:
    """Render the plan that ``/newblock`` is about to commit.

    Shows the per-rung breakdown the trader saw during our design
    discussion: entry, planned SL, lot, planned vs real risk, and
    cumulative real risk. TP prices are intentionally NOT shown —
    they're computed at fill time from the live spread.
    """
    rungs = plan.rungs
    lines = [
        f"📋 <b>План блока</b> — "
        f"<code>{_h(plan.symbol)}</code> <b>{plan.side}</b>",
        f"Якоря: <code>{plan.zero_price}</code> (0%) → "
        f"<code>{plan.hundred_price}</code> (100%)",
        f"SL-шаг: <code>{round(plan.sl_distance, 5)}</code> "
        f"(12.73% × диапазон)",
        f"Базовый риск: <code>${plan.base_risk_usd}</code>  "
        f"Цена отмены: <code>{plan.cancel_price}</code>",
        f"Тип. спред: <code>{round(typical_spread, 5)}</code>",
        "",
        "<pre>",
        f"{'#':>2} {'вход':>10} {'sl_план':>10} {'объём':>7} "
        f"{'риск$':>8} {'факт$':>8}",
    ]
    for r in rungs:
        lines.append(
            f"{r.seq:>2} "
            f"{round(r.entry, 5):>10} "
            f"{round(r.sl, 5):>10} "
            f"{r.lot:>7.2f} "
            f"{r.planned_risk_usd:>8.2f} "
            f"{r.real_risk_usd:>8.2f}"
        )
    lines.append("</pre>")
    lines.extend([
        "",
        f"Планируемый риск: <code>${plan.total_planned_risk()}</code>",
        f"<b>Фактический риск (ROUND UP):</b> "
        f"<code>${plan.total_real_risk()}</code>",
        f"Объём всего: <code>{plan.total_lot()}</code>",
        "",
        "TP рассчитываются автоматически при срабатывании "
        "(по 3 × кумулятивный риск + компенсация спреда).",
    ])
    if plan.note:
        lines.append(f"\nЗаметка: <i>{_h(plan.note)}</i>")
    return "\n".join(lines)


# ============================================================================
# /list and /block <id>
# ============================================================================

def format_block_summary(block: Block) -> str:
    """One-block entry in the /list response."""
    realised = round(sum((o.pnl_usd or 0.0) for o in block.orders), 4)
    head = (
        f"{_status_emoji(block.status)} <b>#{block.id}</b> "
        f"<code>{_h(block.symbol)}</code> <b>{block.side}</b> "
        f"<i>{block.status}</i>"
    )
    time_line = (
        f"   ⏱ <code>{_format_local(block.created_at)}</code> "
        f"({_format_relative(block.created_at)})"
    )
    counter_line = f"   📊 {_block_trade_counter(block)}"
    if realised != 0.0:
        counter_line += f"  💵 <b>{_signed(realised, 4)}</b>"
    cancel_line = f"   ✋ отмена: <code>{block.cancel_price}</code>"
    return "\n".join([head, time_line, counter_line, cancel_line])


def format_block_detail(block: Block) -> str:
    """Verbose view for ``/block <id>``.

    Per-rung table shows live SL/TP (the spread-adjusted ones)
    when the order has filled. Plan-time SL is shown alongside so
    the trader can see the gap.
    """
    total = len(block.orders)
    sorted_orders = sorted(block.orders, key=lambda o: o.seq)

    lines: list[str] = [
        f"{_status_emoji(block.status)} <b>БЛОК #{block.id}</b>",
        f"<code>{_h(block.symbol)}</code> <b>{block.side}</b>",
        f"Статус: <b>{block.status}</b>",
        f"⏱ Открыт: <code>{_format_local(block.created_at)}</code> "
        f"({_format_relative(block.created_at)})",
        f"📊 Сделки: {_block_trade_counter(block)}",
        f"Якоря: <code>{block.zero_price}</code> → "
        f"<code>{block.hundred_price}</code>",
    ]
    cp_state = "активна" if block.cancel_price_active else "выкл"
    lines.append(f"Цена отмены: <code>{block.cancel_price}</code> ({cp_state})")
    if block.note:
        lines.append(f"Заметка: <i>{_h(block.note)}</i>")

    lines.append("")
    lines.append("<b>Ордера:</b>")
    for o in sorted_orders:
        label = _ORDER_STATE_LABEL.get(o.state, str(o.state))
        sl_live = o.sl_price_live if o.sl_price_live is not None else o.sl_price_plan
        tp_live = o.tp_price_live if o.tp_price_live is not None else None
        tp_str = f"<code>{round(tp_live, 5)}</code>" if tp_live is not None else "—"
        pnl_part = ""
        if o.pnl_usd is not None:
            pnl_part = f"  pnl {_signed(o.pnl_usd, 4)}"
        lines.append(
            f"<code>{o.seq}/{total}</code> <b>{label}</b>"
            f"  вход <code>{round(o.entry_price, 5)}</code>"
            f"  TP {tp_str}"
            f"  SL <code>{round(sl_live, 5)}</code>"
            f"  объём <code>{o.lot}</code>"
            f"  риск ${o.real_risk_usd}"
            f"{pnl_part}"
        )

    lines.append("")
    lines.append(
        f"Макс. убыток: <code>${round(sum(o.real_risk_usd for o in sorted_orders), 4)}</code>"
    )
    if block.is_terminal and block.net_pnl is not None:
        lines.append(f"Итоговый PnL: <b>{_signed(block.net_pnl, 4)}</b>")
        if block.closed_at is not None:
            lines.append(
                f"Закрыт: <code>{_format_local(block.closed_at)}</code> "
                f"({_format_relative(block.closed_at)})"
            )
    return "\n".join(lines)


# ============================================================================
# Balance
# ============================================================================

def format_balance(*, balance: float, equity: float, free_margin: float) -> str:
    return (
        "💰 <b>Аккаунт</b>\n"
        f"Баланс: <b>{balance:.2f}</b> USDT\n"
        f"Эквити: <b>{equity:.2f}</b> USDT\n"
        f"Свободная маржа: <b>{free_margin:.2f}</b> USDT"
    )


# ============================================================================
# Statistics
# ============================================================================

def format_stats(stats: StatsSummary) -> str:
    """Render aggregated block stats for the 📊 Статистика screen.

    Empty-state path: when no blocks exist at all, return a single
    friendly line that doubles as a CTA — keeps the chat from
    showing a wall of zeros on first launch.
    """
    if stats.total == 0:
        return (
            "📊 <b>Статистика</b>\n\n"
            "Пока нет ни одного блока. Создайте первый через "
            "<b>📦 Блок → ➕ Создать</b>."
        )

    lines: list[str] = [
        "📊 <b>Статистика</b>",
        "",
        f"Всего блоков: <b>{stats.total}</b>",
        f"  🟡 Активные: <b>{stats.active}</b>",
        f"  🟢 Выигрыши: <b>{stats.wins}</b>",
        f"  🔴 Убытки: <b>{stats.losses}</b>",
    ]
    # Only show INVALID / ERROR rows when they have content — keeps
    # the message tight for users whose blocks all reach a clean end.
    if stats.invalid:
        lines.append(f"  ⚫ Отменены (INVALID): <b>{stats.invalid}</b>")
    if stats.errored:
        lines.append(f"  🚨 Ошибки: <b>{stats.errored}</b>")

    if stats.win_rate is not None:
        lines.append(f"  • <b>Win rate: {stats.win_rate * 100:.1f}%</b>")

    lines.extend([
        "",
        f"💰 Итоговый PnL: <b>{_signed(stats.total_pnl, 2)}</b> USD",
        f"📅 PnL за 7 дней: <b>{_signed(stats.pnl_last_7d, 2)}</b> USD",
    ])

    if stats.by_symbol_top:
        lines.append("")
        lines.append("<b>Топ инструментов:</b>")
        for sym, count, pnl in stats.by_symbol_top:
            # Always show the PnL — even at zero it tells the trader
            # that the symbol was used but hasn't closed any block yet.
            lines.append(
                f"  <code>{_h(sym)}</code> — {count} блок(а), "
                f"PnL <b>{_signed(pnl, 2)}</b>"
            )

    return "\n".join(lines)


# ============================================================================
# Engine notifications → channel/chat messages
# ============================================================================

def render_notification(n: Notification) -> str:  # noqa: PLR0911
    """Render a typed Notification into Telegram-ready HTML.

    Mirrors ``src/bot/formatters.py:render_notification`` so the two
    bots feel like siblings in the chat history.
    """
    p = n.payload

    if n.type == NotificationType.BLOCK_CREATED:
        return (
            f"📦 <b>БЛОК #{n.block_id}</b> создан\n"
            f"Символ: <code>{_h(str(p.get('symbol')))}</code>\n"
            f"Сторона: <b>{p.get('side')}</b>\n"
            f"Ордеров: <b>{p.get('orders')}</b>\n"
            f"Цена отмены: <code>{p.get('cancel_price')}</code>\n"
            f"Макс. риск: <b>${p.get('total_real_risk')}</b>\n"
            f"Статус: <b>ACTIVE</b>"
        )

    if n.type == NotificationType.ORDER_FILLED:
        return (
            f"📍 <b>БЛОК #{n.block_id}</b>\n"
            f"Ордер <b>#{p.get('seq')}</b> сработал по цене "
            f"<code>{p.get('entry')}</code>\n"
            f"SL: <code>{p.get('sl')}</code>  "
            f"TP: <code>{p.get('tp')}</code>  "
            f"Объём: <code>{p.get('lot')}</code>\n"
            f"Спред при заполнении: <code>{p.get('spread')}</code>"
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
        return (
            f"🟢 <b>БЛОК #{n.block_id} WIN</b>\n"
            f"Выигравший ордер: <b>#{p.get('win_order_seq')}</b>\n"
            f"Цена TP: <code>{p.get('tp_price')}</code>\n"
            f"Итоговый PnL: <b>{_signed(p.get('net_pnl') or 0.0, 4)}</b>"
        )

    if n.type == NotificationType.BLOCK_LOSS:
        return (
            f"🔴 <b>БЛОК #{n.block_id} LOSS</b>\n"
            f"Все 6 ордеров остановлены по SL.\n"
            f"Итоговый PnL: <b>{_signed(p.get('net_pnl') or 0.0, 4)}</b>"
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
        net = p.get("net_pnl") or 0.0
        return (
            f"✋ <b>БЛОК #{n.block_id}</b> закрыт вручную.\n"
            f"Итоговый PnL: <b>{_signed(net, 4)}</b>"
        )

    if n.type == NotificationType.SPREAD_ALERT:
        return (
            f"⚠️ <b>БЛОК #{n.block_id}</b>: спред расширился — "
            f"<code>{p.get('spread')}</code> "
            f"(тип. <code>{p.get('typical')}</code>). "
            f"Новые блоки приостановлены."
        )

    # Fallback for unmapped types — never block delivery on a missing
    # template, just dump the payload.
    return f"<b>БЛОК #{n.block_id}</b> — {n.type}: {_h(str(p))}"


# Suppress unused-import lint for the typing alias used in formatters.
__all__ = [
    "format_balance",
    "format_block_detail",
    "format_block_summary",
    "format_plan_preview",
    "format_stats",
    "render_notification",
]

# Convenience re-export to make ``Any`` available without an extra
# import in callers that build a Notification.payload by hand.
_ = Any
