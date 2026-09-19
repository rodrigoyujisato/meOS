"""Telegram integration package for rysOS."""

from rysos.telegram.bot import telegram_bot, TelegramBot
import rysos.telegram.handlers  # Registers message & callback handlers

__all__ = ["telegram_bot", "TelegramBot"]
