"""Message formatting helpers — keep handlers focused on flow logic."""

from __future__ import annotations

from typing import Any

from src.core.notifications import Notification, NotificationType
from src.db import Block, Order
from src.db.enums import BlockSide, BlockStatus, OrderState


# ---------- block / order rendering ----------


def _status_emoji(status: BlockStatus) -> str:
    return {
        BlockStatus.CREATED: "📝",
        BlockStatus.ACTIVE: "🟡",
        BlockStatus.WIN: "🟢",
        BlockStatus.LOSS: "🔴",
        BlockStatus.INVALID: "⚠️",
        BlockStatus.ERROR: "🚨",
    }.get(status, "•")


def _order_state_emoji(state: OrderState) -> str:
    return {
        OrderState.PENDING: "⏳",
        OrderState.TRIGGERED: "📍",
        OrderState.TP_HIT: "🟢",
        OrderState.SL_HIT: "🔴",
        OrderState.CANCELLED: "🚫",
        OrderState.ERROR: "🚨",
    }.get(state, "•")


def format_block_summary(block: Block) -> str:
    """Single-line summary used in lists."""
    return (
        f"{_status_emoji(block.status)} #{block.id} "
        f"{block.symbol} {block.side} "
        f"{block.status} "
        f"cancel={block.cancel_price}"
    )


def format_block_detail(block: Block) -> str:
    """Detailed multi-line view for ``/block <id>``."""
    lines: list[str] = [
        f"{_status_emoji(block.status)} <b>BLOCK #{block.id}</b> — "
        f"<code>{block.symbol}</code> <b>{block.side}</b>",
        f"Status: <b>{block.status}</b>",
        f"Cancel price: <code>{block.cancel_price}</code> "
        f"({'active' if block.cancel_price_active else 'inactive'})",
        f"Created: <code>{block.created_at:%Y-%m-%d %H:%M UTC}</code>",
    ]
    if block.note:
        lines.append(f"Note: <i>{block.note}</i>")
    if block.win_order_seq is not None:
        lines.append(f"Winning rung: <b>#{block.win_order_seq}</b>")
    if block.net_pnl is not None:
        sign = "+" if block.net_pnl >= 0 else ""
        lines.append(f"Net P&amp;L: <b>{sign}{block.net_pnl}</b>")
    if block.closed_at is not None:
        lines.append(f"Closed: <code>{block.closed_at:%Y-%m-%d %H:%M UTC}</code>")

    lines.append("")
    lines.append("<b>Orders:</b>")
    for o in sorted(block.orders, key=lambda x: x.seq):
        lines.append(_format_order_line(o))
    return "\n".join(lines)


def _format_order_line(o: Order) -> str:
    pnl_part = ""
    if o.pnl is not None:
        sign = "+" if o.pnl >= 0 else ""
        pnl_part = f"  pnl {sign}{round(o.pnl, 4)}"
    fill_part = ""
    if o.filled_entry_price is not None:
        fill_part = f"  fill {o.filled_entry_price}"
    return (
        f"{_order_state_emoji(o.state)} "
        f"#{o.seq}  entry <code>{o.entry_price}</code>  "
        f"TP <code>{o.tp_price}</code>  "
        f"SL <code>{o.sl_price}</code>  "
        f"qty <code>{o.qty}</code>  "
        f"<i>{o.state}</i>"
        f"{fill_part}{pnl_part}"
    )


def format_plan_preview(payload: dict[str, Any]) -> str:
    """Render the plan preview during /newblock confirmation."""
    side = payload["side"]
    entries: list[float] = payload["entries"]
    tps: list[float] = payload["tps"]
    sls: list[float] = payload["sls"]
    qty: float = payload["qty"]
    cancel_price: float = payload["cancel_price"]
    symbol: str = payload["symbol"]

    lines = [
        f"📋 <b>Plan preview</b> — <code>{symbol}</code> <b>{side}</b>",
        f"Cancel price: <code>{cancel_price}</code>",
        f"Quantity per rung: <code>{qty}</code>",
        "",
        "<b>Ladder:</b>",
        "<pre>",
        f"{'#':>2} {'entry':>10} {'tp':>10} {'sl':>10}",
    ]
    for i, (e, t, s) in enumerate(zip(entries, tps, sls, strict=True), start=1):
        lines.append(f"{i:>2} {e:>10} {t:>10} {s:>10}")
    lines.append("</pre>")
    return "\n".join(lines)


# ---------- stats / balance ----------


def format_stats(stats: dict[str, Any]) -> str:
    sign = "+" if stats["net_pnl"] >= 0 else ""
    return (
        "📊 <b>Statistics</b>\n"
        f"Closed blocks: <b>{stats['total_closed']}</b>\n"
        f"🟢 Wins: <b>{stats['wins']}</b> "
        f"({stats['win_rate_pct']}%)\n"
        f"🔴 Losses: <b>{stats['losses']}</b>\n"
        f"⚠️ Invalid: <b>{stats['invalids']}</b>\n"
        f"🚨 Errors: <b>{stats['errors']}</b>\n"
        f"\nNet P&amp;L: <b>{sign}{stats['net_pnl']}</b> USDT"
    )


def format_balance(usdt: float) -> str:
    return f"💰 Wallet balance: <b>{usdt:.4f} USDT</b>"


# ---------- notifier rendering ----------


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
        sign = "+" if pnl >= 0 else ""
        return (
            f"🟥 <b>BLOCK #{n.block_id}</b>\n"
            f"Order <b>#{p.get('seq')}</b> SL hit at "
            f"<code>{p.get('price')}</code> "
            f"({sign}{round(pnl, 4)})"
        )
    if n.type == NotificationType.BLOCK_WIN:
        net = p.get("net_pnl") or 0.0
        sign = "+" if net >= 0 else ""
        return (
            f"🟢 <b>BLOCK #{n.block_id} WIN</b>\n"
            f"Winning rung: <b>#{p.get('win_order_seq')}</b>\n"
            f"TP price: <code>{p.get('tp_price')}</code>\n"
            f"Net P&amp;L: <b>{sign}{round(net, 4)}</b>\n"
            f"Pending orders cancelled."
        )
    if n.type == NotificationType.BLOCK_LOSS:
        net = p.get("net_pnl") or 0.0
        return (
            f"🔴 <b>BLOCK #{n.block_id} LOSS</b>\n"
            f"All 8 orders stopped out.\n"
            f"Net P&amp;L: <b>{round(net, 4)}</b>"
        )
    if n.type == NotificationType.BLOCK_INVALID:
        cp = p.get("cancel_price")
        mp = p.get("mark_price")
        extra = ""
        if cp is not None and mp is not None:
            extra = f"\nCancel price: <code>{cp}</code>, market: <code>{mp}</code>"
        return (
            f"⚠️ <b>BLOCK #{n.block_id} INVALID</b>\n"
            f"All pending orders cancelled.{extra}"
        )
    if n.type == NotificationType.BLOCK_ERROR:
        return (
            f"🚨 <b>BLOCK #{n.block_id} ERROR</b>\n"
            f"Reason: {p.get('reason')}\n"
            f"Manual review required."
        )
    if n.type == NotificationType.BLOCK_MANUAL_CLOSE:
        return f"✋ <b>BLOCK #{n.block_id}</b> closed manually."
    return f"BLOCK #{n.block_id} — {n.type}: {n.payload}"
