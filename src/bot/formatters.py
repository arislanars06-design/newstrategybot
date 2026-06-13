"""Message formatting helpers — keep handlers focused on flow logic.

Most formatters return Telegram-ready HTML. The few that contain raw
user-supplied content (block notes) escape it explicitly so the parser
never trips on accidental ``<`` / ``>`` characters.
"""

from __future__ import annotations

from html import escape as _h
from typing import Any

from src.core.notifications import Notification, NotificationType
from src.core.risk import order_risk_amount, total_block_risk
from src.db import Block, Order
from src.db.enums import BlockSide, BlockStatus, OrderState


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


# Short tag the user wants in the per-rung list ("1/8 SL"). The
# trader's vocabulary is slightly different from the engine's:
# TRIGGERED (the position is open and waiting on its TP/SL) reads as
# "ACTIVE" in their world, while a CREATED-but-not-yet-placed rung
# reads as "PENDING".
_ORDER_STATE_LABEL: dict[OrderState, str] = {
    OrderState.PENDING: "PENDING",
    OrderState.TRIGGERED: "ACTIVE",
    OrderState.TP_HIT: "TP",
    OrderState.SL_HIT: "SL",
    OrderState.CANCELLED: "CANCEL",
    OrderState.ERROR: "ERROR",
}


def _signed(amount: float, places: int = 2) -> str:
    """Render a number with an explicit +/- sign."""
    sign = "+" if amount >= 0 else ""
    return f"{sign}{round(amount, places)}"


# =============================================================================
# Block / order rendering
# =============================================================================


def format_block_summary(block: Block) -> str:
    """Single-line summary used in the /list response."""
    return (
        f"{_status_emoji(block.status)} #{block.id} "
        f"{block.symbol} {block.side} "
        f"{block.status} "
        f"cancel={block.cancel_price}"
    )


def format_block_detail(
    block: Block, *, realtime_pnl: dict[str, Any] | None = None
) -> str:
    """Detailed multi-line view for ``/block <id>``.

    The layout matches the spec the trader requested:

        Block #15
        BTCUSDT BUY
        Status: ACTIVE

        Orders:
        1/8 SL
        2/8 ACTIVE
        ...

        Current PNL: +12.5$

    ``realtime_pnl`` is the dict returned by
    :meth:`BlockEngine.compute_block_realtime_pnl`. When the block is
    in a terminal state we fall back to the persisted ``net_pnl``.
    """
    total = len(block.orders)
    lines: list[str] = [
        f"{_status_emoji(block.status)} <b>BLOCK #{block.id}</b>",
        f"<code>{block.symbol}</code> <b>{block.side}</b>",
        f"Status: <b>{block.status}</b>",
    ]

    if block.note:
        lines.append(f"Note: <i>{_h(block.note)}</i>")

    # Cancel-price / win-rung header — small, only shown when meaningful.
    cp_state = "active" if block.cancel_price_active else "off"
    lines.append(f"Cancel price: <code>{block.cancel_price}</code> ({cp_state})")
    if block.win_order_seq is not None:
        lines.append(f"Winning rung: <b>#{block.win_order_seq}</b>")

    # Spec-shaped order list.
    lines.append("")
    lines.append("<b>Orders:</b>")
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
            f"  entry <code>{o.entry_price}</code>"
            f"  TP <code>{o.tp_price}</code>"
            f"  SL <code>{o.sl_price}</code>"
            f"  qty <code>{o.qty}</code>"
            f"  risk <code>{round(risk, 4)}</code>"
            f"{pnl_part}"
        )

    # Risk + PnL summary block.
    lines.append("")
    block_risk = total_block_risk(
        side=block.side,
        rungs=((o.entry_price, o.sl_price, o.qty) for o in sorted_orders),
    )
    lines.append(f"Block risk (max loss): <code>{block_risk}</code>")

    if block.is_terminal and block.net_pnl is not None:
        lines.append(f"Net PnL: <b>{_signed(block.net_pnl, 4)}</b>")
    elif realtime_pnl is not None:
        unr = realtime_pnl.get("unrealised")
        if unr is None:
            lines.append(
                f"Realised PnL: <b>{_signed(realtime_pnl['realised'], 4)}</b>"
                "  (mark price unavailable)"
            )
        else:
            lines.append(
                f"Current PnL: <b>{_signed(realtime_pnl['total'], 4)}</b>"
                f"  (realised {_signed(realtime_pnl['realised'], 4)},"
                f" unrealised {_signed(unr, 4)})"
            )
            if realtime_pnl.get("mark_price") is not None:
                lines.append(
                    f"Mark price: <code>{realtime_pnl['mark_price']}</code>"
                    f"  open positions: <b>{realtime_pnl['open_count']}</b>"
                )

    lines.append("")
    lines.append(f"Created: <code>{block.created_at:%Y-%m-%d %H:%M UTC}</code>")
    if block.closed_at is not None:
        lines.append(f"Closed: <code>{block.closed_at:%Y-%m-%d %H:%M UTC}</code>")

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
        f"📋 <b>Plan preview</b> — <code>{symbol}</code> <b>{side}</b>",
        f"Cancel price: <code>{cancel_price}</code>",
        f"Quantity per rung: <code>{qty}</code>",
        f"Block risk (max loss): <code>{block_risk}</code>",
        "",
        "<b>Ladder:</b>",
        "<pre>",
        f"{'#':>2} {'entry':>10} {'tp':>10} {'sl':>10} {'risk':>8}",
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
        f"🔍 <b>Discovered ladder</b> — <code>{symbol}</code> <b>{side}</b>",
        f"Rungs: <b>{len(rungs)}</b>",
        f"Block risk (max loss): <code>{block_risk}</code>",
        "<pre>",
        f"{'#':>2} {'entry':>10} {'tp':>10} {'sl':>10} {'qty':>10} {'risk':>8}",
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


def _stats_window_label(days: int | None) -> str:
    if days is None:
        return "All time"
    if days == 1:
        return "Today (last 24h)"
    return f"Last {days} days"


def format_stats(stats: dict[str, Any]) -> str:
    label = _stats_window_label(stats.get("window_days"))
    return (
        f"📊 <b>Statistics</b> — {label}\n"
        f"Closed blocks: <b>{stats['total_closed']}</b>\n"
        f"🟢 Wins: <b>{stats['wins']}</b> "
        f"({stats['win_rate_pct']}%)\n"
        f"🔴 Losses: <b>{stats['losses']}</b>\n"
        f"⚫ Invalid: <b>{stats['invalids']}</b>\n"
        f"🚨 Errors: <b>{stats['errors']}</b>\n"
        f"\nNet PnL: <b>{_signed(stats['net_pnl'], 4)}</b> USDT"
    )


def format_report(rows: list[dict[str, Any]], *, days: int) -> str:
    if not rows:
        return f"📈 <b>Reports</b> — last {days} days\n\nNo closed blocks in this window."
    lines = [f"📈 <b>Reports</b> — last {days} days", "", "<pre>"]
    lines.append(f"{'date':<12} {'W':>3} {'L':>3} {'I':>3} {'E':>3} {'net':>10}")
    for r in rows:
        lines.append(
            f"{r['date']:<12} "
            f"{r['wins']:>3} {r['losses']:>3} "
            f"{r['invalids']:>3} {r['errors']:>3} "
            f"{_signed(r['net_pnl'], 4):>10}"
        )
    lines.append("</pre>")
    total_pnl = round(sum(r["net_pnl"] for r in rows), 4)
    lines.append(f"\nTotal PnL: <b>{_signed(total_pnl, 4)}</b> USDT")
    return "\n".join(lines)


def format_balance(usdt: float) -> str:
    return f"💰 Wallet balance: <b>{usdt:.4f} USDT</b>"


# =============================================================================
# Notifier rendering (channel + chat)
# =============================================================================


def render_notification(n: Notification) -> str:
    p = n.payload
    if n.type == NotificationType.BLOCK_CREATED:
        return (
            f"📦 <b>BLOCK #{n.block_id}</b> created\n"
            f"Symbol: <code>{p.get('symbol')}</code>\n"
            f"Side: <b>{p.get('side')}</b>\n"
            f"Orders: <b>{p.get('orders')}</b>\n"
            f"Cancel price: <code>{p.get('cancel_price')}</code>\n"
            f"Status: <b>ACTIVE</b>"
        )
    if n.type == NotificationType.ORDER_TRIGGERED:
        return (
            f"📍 <b>BLOCK #{n.block_id}</b>\n"
            f"Order <b>#{p.get('seq')}</b> triggered at "
            f"<code>{p.get('price')}</code>\n"
            f"Position OPEN (qty <code>{p.get('qty')}</code>)"
        )
    if n.type == NotificationType.SL_HIT:
        pnl = p.get("pnl") or 0.0
        return (
            f"🟥 <b>BLOCK #{n.block_id}</b>\n"
            f"Order <b>#{p.get('seq')}</b> SL hit at "
            f"<code>{p.get('price')}</code> "
            f"({_signed(pnl, 4)})"
        )
    if n.type == NotificationType.BLOCK_WIN:
        net = p.get("net_pnl") or 0.0
        return (
            f"🟢 <b>BLOCK #{n.block_id} WIN</b>\n"
            f"Winning rung: <b>#{p.get('win_order_seq')}</b>\n"
            f"TP price: <code>{p.get('tp_price')}</code>\n"
            f"Net PnL: <b>{_signed(net, 4)}</b>\n"
            f"Pending orders cancelled."
        )
    if n.type == NotificationType.BLOCK_LOSS:
        net = p.get("net_pnl") or 0.0
        return (
            f"🔴 <b>BLOCK #{n.block_id} LOSS</b>\n"
            f"All 8 orders stopped out.\n"
            f"Net PnL: <b>{_signed(net, 4)}</b>"
        )
    if n.type == NotificationType.BLOCK_INVALID:
        cp = p.get("cancel_price")
        mp = p.get("mark_price")
        extra = ""
        if cp is not None and mp is not None:
            extra = f"\nCancel price: <code>{cp}</code>, market: <code>{mp}</code>"
        return (
            f"⚫ <b>BLOCK #{n.block_id} INVALID</b>\n"
            f"All pending orders cancelled.{extra}"
        )
    if n.type == NotificationType.BLOCK_ERROR:
        return (
            f"🚨 <b>BLOCK #{n.block_id} ERROR</b>\n"
            f"Reason: {_h(str(p.get('reason')))}\n"
            f"Manual review required."
        )
    if n.type == NotificationType.BLOCK_MANUAL_CLOSE:
        return f"✋ <b>BLOCK #{n.block_id}</b> closed manually."
    return f"BLOCK #{n.block_id} — {n.type}: {n.payload}"



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
        f"📋 <b>Fib plan</b> — <code>{symbol}</code> <b>{side_str}</b>",
        f"Range: <code>{zero_price}</code> (0%) → "
        f"<code>{hundred_price}</code> (100%)  "
        f"Δ {range_size:+.2f}",
        f"First risk: <code>{first_risk_usd}</code> USDT  "
        f"Leverage: <code>{leverage}x</code>  "
        f"Cancel price: <code>{cancel_price}</code>",
        "",
        "<pre>",
        f"{'#':>2} {'entry':>10} {'tp':>10} {'sl':>10} "
        f"{'qty':>10} {'risk$':>7} {'sl%':>5}",
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
        f"<b>Total max risk:</b> <code>{round(total_risk, 2)}</code> USDT  "
        "(if every SL fires)",
        f"<b>Total margin:</b> <code>{round(total_margin, 2)}</code> USDT  "
        "(collateral required)",
        f"<b>Total notional:</b> <code>{round(total_pos, 2)}</code> USDT",
    ])
    return "\n".join(lines)
