"""Engine, watchers, and runtime guards.

This package is the "operational" half of the bot. Strategy math
lives next door in :mod:`futures_bot.strategy`; here we deal with
broker side-effects, timers, and the rules that decide *when* the
strategy is allowed to run (spread sanity, trading hours, news
blackouts).
"""
