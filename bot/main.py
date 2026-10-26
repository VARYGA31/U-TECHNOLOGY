"""Запуск бота: polling, команды /start /card /new."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from .config import DATA_DIR, settings
from .handlers import router


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать / 开始"),
        BotCommand(command="card", description="Карточка КП / 报价单卡片"),
        BotCommand(command="new", description="Новое КП / 新报价单"),
        BotCommand(command="diag", description="Проверка настроек / 配置检查"),
    ])


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not settings.bot_token:
        raise SystemExit("Не задан BOT_TOKEN — добавьте его в .env")
    if not settings.ai_enabled:
        logging.warning("OPENAI_API_KEY не задан: только свой формат КП, без перевода")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    bot = Bot(token=settings.bot_token,
              default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp.include_router(router)

    await set_commands(bot)
    logging.info("Бот запущен")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
