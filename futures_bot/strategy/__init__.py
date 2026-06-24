"""Strategy package: Fibonacci levels, plan builder, risk and TP math.

This is the pure-math heart of the futures bot. Everything in here is
broker-agnostic: it computes prices, lot sizes, and risk numbers from
the user's inputs without touching MT5, Telegram, or the DB. Keeping it
isolated lets us unit-test the strategy without spinning up the rest of
the system.
"""
