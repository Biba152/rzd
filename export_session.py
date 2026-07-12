"""
Локальный одноразовый экспорт авторизованной сессии РЖД.

Установка:
    pip install playwright
    playwright install chromium

Запуск:
    python export_session.py

После ручного входа получится storage_state.json.
Загрузите его в веб-панели: Настройки → Импорт локальной сессии РЖД.
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT = Path("storage_state.json")


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(locale="ru-RU")
        page = await context.new_page()
        await page.goto("https://ticket.rzd.ru/", wait_until="domcontentloaded")
        print(
            "\nВойдите в РЖД в открывшемся окне и решите все проверки.\n"
            "Когда личный кабинет будет открыт, вернитесь сюда."
        )
        input("Нажмите Enter для сохранения сессии: ")
        await context.storage_state(path=str(OUTPUT))
        await browser.close()
        print(f"Готово: {OUTPUT.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
