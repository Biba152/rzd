from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse
from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

# ---------------------------------------------------------------------------
# Единственная конфигурация сервиса. Пользователей/поездки через сайт добавить
# нельзя: сценарий всегда работает только с этими двумя сегментами и пассажиром.
# ---------------------------------------------------------------------------

PORTAL_HOME = "https://www.rzd.ru/"
TICKET_HOME = "https://ticket.rzd.ru/"
OUTBOUND_SEATS_URL = "https://ticket.rzd.ru/booking/rail;route=005/seats"
OUTBOUND_BOARDING_URL = "https://ticket.rzd.ru/booking/rail;route=005/boarding"
RETURN_SEARCH_URL = (
    "https://ticket.rzd.ru/searchresults/v/1/"
    "5a13bd35340c745ca1e888a1/5a13bd30340c745ca1e88771/"
    "2026-07-25/2026-07-29?aim=return-trip&adult=1"
)
RETURN_SEATS_URL = "https://ticket.rzd.ru/booking/rail;route=006/seats"
RETURN_BOARDING_URL = "https://ticket.rzd.ru/booking/rail;route=006/boarding"

OUTBOUND = {
    "from": "Владивосток",
    "to": "Хабаровск-1",
    "date": "25.07.2026",
    "date_iso": "2026-07-25",
    "train": "005Э",
    "wagon": "05",
    "seat": "35",
}
RETURN = {
    "from": "Хабаровск-1",
    "to": "Владивосток",
    "date": "29.07.2026",
    "date_iso": "2026-07-29",
    "train": "006Э",
    "departure_time": "19:40",
    "car_type": "Купе",
    "wagon": "07",
    "seat": "35",
}
PASSENGER = {
    "surname": "ДЕНИСЕНКО",
    "name": "ИГОРЬ",
    "patronymic": "ПАВЛОВИЧ",
}

# Значения по умолчанию уже встроены, чтобы на Render не пришлось вручную
# создавать переменные окружения. При необходимости любое значение можно
# переопределить переменной окружения с тем же именем.
DEFAULT_RZD_LOGIN = "DedushkaPopa"
DEFAULT_RZD_PASSWORD = "DedushkaSO67"
DEFAULT_TELEGRAM_BOT_TOKEN = "8848029929:AAEJwEh8cQh8PsgCrersdA0gYGArQfT1pW0"
# Российский HTTP-прокси применяется только к Chromium/сайту РЖД.
# Telegram API и endpoint FastAPI продолжают работать напрямую через Render.
DEFAULT_RZD_PROXY_SERVER = "http://193.239.86.180:80"
DEFAULT_ALLOWED_TELEGRAM_IDS = {
    1143838304,
    5317465,
    5461520961,
    958854457,
}

RZD_LOGIN = os.getenv("RZD_LOGIN", DEFAULT_RZD_LOGIN).strip()
RZD_PASSWORD = os.getenv("RZD_PASSWORD", DEFAULT_RZD_PASSWORD).strip()
TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", DEFAULT_TELEGRAM_BOT_TOKEN
).strip()
RZD_PROXY_SERVER = os.getenv(
    "RZD_PROXY_SERVER", DEFAULT_RZD_PROXY_SERVER
).strip()
RZD_PROXY_USERNAME = os.getenv("RZD_PROXY_USERNAME", "").strip()
RZD_PROXY_PASSWORD = os.getenv("RZD_PROXY_PASSWORD", "").strip()


def normalize_proxy_server(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "://" not in value:
        return "http://" + value
    return value


def browser_proxy_config() -> dict[str, str] | None:
    server = normalize_proxy_server(RZD_PROXY_SERVER)
    if not server:
        return None
    proxy: dict[str, str] = {"server": server}
    if RZD_PROXY_USERNAME:
        proxy["username"] = RZD_PROXY_USERNAME
    if RZD_PROXY_PASSWORD:
        proxy["password"] = RZD_PROXY_PASSWORD
    return proxy


def proxy_label() -> str:
    server = normalize_proxy_server(RZD_PROXY_SERVER)
    return server.removeprefix("http://").removeprefix("https://") if server else "выключен"


def friendly_browser_error(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    upper = text.upper()
    if "ERR_PROXY_CONNECTION_FAILED" in upper or "ERR_TUNNEL_CONNECTION_FAILED" in upper:
        return f"Российский прокси {proxy_label()} недоступен или не поддерживает HTTPS CONNECT."
    if "ERR_CONNECTION_REFUSED" in upper and RZD_PROXY_SERVER:
        return f"Соединение через российский прокси {proxy_label()} отклонено."
    if "ERR_TIMED_OUT" in upper or "TIMEOUT" in upper:
        if RZD_PROXY_SERVER:
            return f"РЖД не ответил через российский прокси {proxy_label()} вовремя."
        return "РЖД не ответил вовремя."
    if "ERR_NAME_NOT_RESOLVED" in upper:
        return "Не удалось разрешить адрес РЖД через настроенное соединение."
    return text


def parse_allowed_telegram_ids(raw: str | None) -> set[int]:
    if not raw or not raw.strip():
        return set(DEFAULT_ALLOWED_TELEGRAM_IDS)
    result: set[int] = set()
    for part in re.split(r"[,;\s]+", raw.strip()):
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError as exc:
            raise RuntimeError(
                f"Некорректный Telegram ID в TELEGRAM_ALLOWED_IDS: {part!r}"
            ) from exc
    return result


ALLOWED_TELEGRAM_IDS = parse_allowed_telegram_ids(
    os.getenv("TELEGRAM_ALLOWED_IDS")
)
CHECK_INTERVAL_HOURS = max(1, int(os.getenv("CHECK_INTERVAL_HOURS", "2")))
RUN_ON_STARTUP = os.getenv("RUN_ON_STARTUP", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
HEADLESS = os.getenv("HEADLESS", "true").lower() in {"1", "true", "yes", "on"}
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Vladivostok")

DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/rzd-runtime"))
PROFILE_DIR = DATA_DIR / "browser-profile"
SCREENSHOT_DIR = DATA_DIR / "screenshots"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("rzd-single-user")

scheduler = AsyncIOScheduler(timezone="UTC")
service: "RzdAutomation | None" = None
telegram_bot: Application | None = None
run_lock = asyncio.Lock()

status: dict[str, Any] = {
    "state": "starting",
    "message": "Сервис запускается.",
    "automation_enabled": True,
    "last_started_at": None,
    "last_finished_at": None,
    "last_success_at": None,
    "next_due_at": None,
    "last_screenshot": None,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def require_configuration() -> None:
    missing = []
    if not RZD_LOGIN:
        missing.append("RZD_LOGIN")
    if not RZD_PASSWORD:
        missing.append("RZD_PASSWORD")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not ALLOWED_TELEGRAM_IDS:
        missing.append("TELEGRAM_ALLOWED_IDS")
    if missing:
        raise RuntimeError("Не заданы параметры: " + ", ".join(missing))


async def telegram_text(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not ALLOWED_TELEGRAM_IDS:
        logger.info("Telegram не настроен: %s", message.replace("\n", " | "))
        return
    async with httpx.AsyncClient(timeout=20) as client:
        for chat_id in sorted(ALLOWED_TELEGRAM_IDS):
            try:
                response = await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    data={
                        "chat_id": chat_id,
                        "text": message,
                        "disable_web_page_preview": "true",
                    },
                )
                response.raise_for_status()
            except Exception as exc:
                logger.warning(
                    "Не удалось отправить Telegram пользователю %s: %s",
                    chat_id,
                    type(exc).__name__,
                )


async def telegram_photo(path: Path, caption: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not ALLOWED_TELEGRAM_IDS or not path.exists():
        return
    async with httpx.AsyncClient(timeout=30) as client:
        for chat_id in sorted(ALLOWED_TELEGRAM_IDS):
            try:
                with path.open("rb") as image:
                    response = await client.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                        data={"chat_id": chat_id, "caption": caption[:1024]},
                        files={"photo": (path.name, image, "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png")},
                    )
                response.raise_for_status()
            except Exception as exc:
                logger.warning(
                    "Не удалось отправить скриншот пользователю %s: %s",
                    chat_id,
                    type(exc).__name__,
                )


class RzdAutomation:
    def __init__(self) -> None:
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None

    async def start(self) -> None:
        if self.context is not None:
            return
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        # Persistent context хранит cookies/localStorage в течение жизни Render-
        # процесса. После сна/redeploy /tmp исчезает, поэтому вход выполнится снова.
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            proxy=browser_proxy_config(),
            locale="ru-RU",
            timezone_id=APP_TIMEZONE,
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self.context.set_default_timeout(15_000)

    async def close(self) -> None:
        if self.context is not None:
            await self.context.close()
            self.context = None
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None

    async def page(self) -> Page:
        if self.context is None:
            await self.start()
        assert self.context is not None
        page = await self.context.new_page()
        page.set_default_navigation_timeout(75_000)
        return page

    async def screenshot(self, page: Page, name: str) -> Path | None:
        """Сохраняет диагностический экран, но никогда не ломает сценарий.

        Полностраничные PNG на тяжёлой странице РЖД иногда зависают дольше
        action timeout. Сначала снимаем только видимую область в JPEG, а при
        сбое используем CDP напрямую. Если оба способа не сработали, просто
        продолжаем без изображения и сохраняем исходный результат операции.
        """
        safe = re.sub(r"[^a-zA-Zа-яА-Я0-9_-]", "_", name)[:70]
        path = SCREENSHOT_DIR / f"{utc_now().strftime('%Y%m%d_%H%M%S')}_{safe}.jpg"

        try:
            await page.screenshot(
                path=str(path),
                type="jpeg",
                quality=65,
                full_page=False,
                animations="disabled",
                caret="hide",
                timeout=8_000,
            )
            return path
        except Exception as exc:
            logger.warning("Обычный скриншот не создан: %s", exc)

        try:
            assert self.context is not None
            session = await self.context.new_cdp_session(page)
            result = await asyncio.wait_for(
                session.send(
                    "Page.captureScreenshot",
                    {
                        "format": "jpeg",
                        "quality": 55,
                        "captureBeyondViewport": False,
                        "fromSurface": True,
                    },
                ),
                timeout=6,
            )
            path.write_bytes(base64.b64decode(result["data"]))
            await session.detach()
            return path
        except Exception as exc:
            logger.warning("Резервный скриншот не создан: %s", exc)
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return None

    @staticmethod
    async def visible(locator: Locator, timeout: int = 900) -> bool:
        try:
            return await locator.is_visible(timeout=timeout)
        except Exception:
            return False

    async def first_visible(self, locators: list[Locator]) -> Locator | None:
        for locator in locators:
            try:
                count = min(await locator.count(), 30)
                for index in range(count):
                    item = locator.nth(index)
                    if await self.visible(item):
                        return item
            except Exception:
                continue
        return None

    async def click_first(self, locators: list[Locator], *, force: bool = False) -> bool:
        item = await self.first_visible(locators)
        if item is None:
            return False
        try:
            await item.scroll_into_view_if_needed()
            if not force and not await item.is_enabled(timeout=1000):
                return False
            await item.click(force=force)
            return True
        except Exception:
            try:
                await item.evaluate("el => el.click()")
                return True
            except Exception:
                return False

    async def fill_first(self, locators: list[Locator], value: str) -> bool:
        item = await self.first_visible(locators)
        if item is None:
            return False
        try:
            await item.click()
            await item.fill(value)
            return True
        except Exception:
            return False

    async def dismiss_popups(self, page: Page) -> None:
        pattern = re.compile(
            r"^(Принять|Согласен|Хорошо|Понятно|Закрыть|Продолжить без файлов cookie)$",
            re.I,
        )
        await self.click_first(
            [
                page.get_by_role("button", name=pattern),
                page.get_by_text(pattern, exact=True),
            ]
        )

    async def wait_app(self, page: Page, delay_ms: int = 3500) -> None:
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(delay_ms)
        await self.dismiss_popups(page)

    async def is_logged_in(self, page: Page) -> bool:
        # Не считаем обычную иконку профиля признаком входа: она присутствует и
        # у гостя. Проверяем только элементы, появляющиеся у авторизованного.
        positive = [
            page.get_by_text(re.compile(r"Мои заказы|Мои поездки|Выйти|Выход", re.I)),
            page.locator('[href*="logout" i], .j-profile-logout'),
            page.locator('.j-profile-username').filter(has_text=re.compile(r"\S+")),
            page.get_by_role("link", name=re.compile(r"Мои заказы|Личный кабинет пассажира", re.I)),
        ]
        return await self.first_visible(positive) is not None

    async def login_once(self, page: Page) -> None:
        # Авторизация выполняется на основном портале. После неё cookie-сессия
        # используется билетным приложением ticket.rzd.ru.
        await page.goto(PORTAL_HOME, wait_until="domcontentloaded")
        await self.wait_app(page, 3000)
        if await self.is_logged_in(page):
            return

        clicked = await self.click_first(
            [
                page.get_by_role("button", name=re.compile(r"^Войти$", re.I)),
                page.get_by_role("link", name=re.compile(r"^Войти$", re.I)),
                page.get_by_text(re.compile(r"^Войти$", re.I), exact=True),
                page.locator('[data-test-id="profile"], .j-login'),
            ]
        )
        if not clicked:
            raise RuntimeError("Не найдена кнопка входа РЖД.")
        await page.wait_for_timeout(1800)

        login_ok = await self.fill_first(
            [
                page.get_by_label(re.compile(r"Логин|Телефон|E-mail|Почта", re.I)),
                page.locator(
                    'input[name="j_username"], input[name*="login" i], '
                    'input[name*="username" i], input[type="email"], input[type="tel"]'
                ),
            ],
            RZD_LOGIN,
        )
        password_ok = await self.fill_first(
            [
                page.get_by_label(re.compile(r"Пароль", re.I)),
                page.locator('input[name="j_password"], input[type="password"]'),
            ],
            RZD_PASSWORD,
        )
        if not login_ok or not password_ok:
            raise RuntimeError("Не найдены поля логина/пароля РЖД.")

        if not await self.click_first(
            [
                page.get_by_role("button", name=re.compile(r"Войти|Продолжить", re.I)),
                page.locator('button[type="submit"]'),
            ]
        ):
            raise RuntimeError("Не найдена кнопка подтверждения входа.")

        await page.wait_for_timeout(4500)
        if await self.is_logged_in(page):
            return

        challenge = await self.first_visible(
            [
                page.get_by_label(re.compile(r"Код|Captcha|Капча|SMS", re.I)),
                page.locator(
                    'input[name*="captcha" i], input[placeholder*="код" i], '
                    'input[name*="code" i]'
                ),
            ]
        )
        if challenge is not None:
            raise RuntimeError("РЖД запросил CAPTCHA или код подтверждения.")
        raise RuntimeError("РЖД не подтвердил авторизацию.")

    async def goto(self, page: Page, url: str, delay_ms: int = 4500) -> None:
        await page.goto(url, wait_until="domcontentloaded")
        await self.wait_app(page, delay_ms)

    @staticmethod
    def station_pattern(value: str) -> re.Pattern[str]:
        # На сайте встречаются варианты «Хабаровск 1» и «Хабаровск-1».
        escaped = re.escape(value).replace(r"\-", r"[\s-]?")
        return re.compile(escaped, re.I)

    async def fill_station(self, page: Page, direction: str, value: str) -> bool:
        if direction == "from":
            label = re.compile(r"Откуда|Станция отправления|Пункт отправления", re.I)
            css = (
                'input[placeholder*="Откуда" i], input[placeholder*="отправ" i], '
                'input[name*="from" i], input[data-test-id*="from" i]'
            )
        else:
            label = re.compile(r"Куда|Станция прибытия|Пункт назначения", re.I)
            css = (
                'input[placeholder*="Куда" i], input[placeholder*="прибыт" i], '
                'input[name*="to" i], input[data-test-id*="to" i]'
            )

        for frame in page.frames:
            field = await self.first_visible(
                [
                    frame.get_by_label(label),
                    frame.get_by_role("textbox", name=label),
                    frame.locator(css),
                ]
            )
            if field is None:
                continue
            try:
                await field.click()
                await field.fill(value)
                await page.wait_for_timeout(1100)

                pattern = self.station_pattern(value)
                selected = await self.click_first(
                    [
                        frame.get_by_role("option", name=pattern),
                        frame.locator('[role="listbox"] [role="option"]').filter(
                            has_text=pattern
                        ),
                        frame.locator(
                            '[class*="suggest" i] li, [class*="autocomplete" i] li, '
                            '[class*="dropdown" i] li'
                        ).filter(has_text=pattern),
                    ]
                )
                if not selected:
                    # Если подсказка не имеет ARIA-разметки, выбираем первую
                    # клавишами — виджет РЖД обычно принимает ArrowDown + Enter.
                    await field.press("ArrowDown")
                    await field.press("Enter")
                await page.wait_for_timeout(450)
                return True
            except Exception:
                continue
        return False

    async def fill_date_field(
        self,
        field: Locator,
        display_value: str,
        iso_value: str,
    ) -> bool:
        try:
            input_type = (await field.get_attribute("type") or "").lower()
            value = iso_value if input_type == "date" else display_value
            await field.click()
            await field.fill(value)
            await field.press("Tab")
            return True
        except Exception:
            try:
                await field.click()
                await field.press("Control+A")
                await field.type(display_value, delay=45)
                await field.press("Tab")
                return True
            except Exception:
                return False

    async def ensure_round_trip_mode(self, page: Page) -> None:
        # Виджет иногда открывается в режиме «В одну сторону». Переключаем его,
        # только если видна явная кнопка/вкладка поездки туда-обратно.
        mode_pattern = re.compile(r"Туда\s*и\s*обратно|Туда-обратно|Обратный билет", re.I)
        for frame in page.frames:
            if await self.click_first(
                [
                    frame.get_by_role("button", name=mode_pattern),
                    frame.get_by_role("tab", name=mode_pattern),
                    frame.get_by_role("radio", name=mode_pattern),
                    frame.get_by_text(mode_pattern, exact=True),
                ]
            ):
                await page.wait_for_timeout(700)
                return

    async def fill_trip_dates(self, page: Page) -> bool:
        await self.ensure_round_trip_mode(page)
        outbound_label = re.compile(
            r"Туда|Дата отправления|Дата поездки|Отправление", re.I
        )
        return_label = re.compile(
            r"Обратно|Дата возвращения|Дата обратной поездки|Возвращение", re.I
        )

        for frame in page.frames:
            outbound = await self.first_visible(
                [
                    frame.get_by_label(outbound_label),
                    frame.get_by_role("textbox", name=outbound_label),
                    frame.locator(
                        'input[name*="departure" i], input[data-test-id*="departure" i], '
                        'input[placeholder*="Туда" i]'
                    ),
                ]
            )
            returning = await self.first_visible(
                [
                    frame.get_by_label(return_label),
                    frame.get_by_role("textbox", name=return_label),
                    frame.locator(
                        'input[name*="return" i], input[data-test-id*="return" i], '
                        'input[placeholder*="Обратно" i]'
                    ),
                ]
            )
            if outbound is not None and returning is not None:
                first_ok = await self.fill_date_field(
                    outbound, OUTBOUND["date"], OUTBOUND["date_iso"]
                )
                second_ok = await self.fill_date_field(
                    returning, RETURN["date"], RETURN["date_iso"]
                )
                if first_ok and second_ok:
                    return True

        # Резерв: в виджете даты могут быть двумя безымянными полями подряд.
        for frame in page.frames:
            date_inputs = frame.locator(
                'input[type="date"], input[placeholder*="дд.мм" i], '
                'input[placeholder*="Дата" i]'
            )
            visible: list[Locator] = []
            try:
                for index in range(min(await date_inputs.count(), 12)):
                    item = date_inputs.nth(index)
                    if await self.visible(item):
                        visible.append(item)
            except Exception:
                continue
            if len(visible) >= 2:
                first_ok = await self.fill_date_field(
                    visible[0], OUTBOUND["date"], OUTBOUND["date_iso"]
                )
                second_ok = await self.fill_date_field(
                    visible[1], RETURN["date"], RETURN["date_iso"]
                )
                if first_ok and second_ok:
                    return True
        return False

    async def search_round_trip(self, page: Page) -> None:
        # После входа возвращаемся на www.rzd.ru и формируем маршрут с нуля.
        await self.goto(page, PORTAL_HOME, 5500)
        if not await self.fill_station(page, "from", OUTBOUND["from"]):
            raise RuntimeError("Поле «Откуда» на главной странице не найдено.")
        if not await self.fill_station(page, "to", OUTBOUND["to"]):
            raise RuntimeError("Поле «Куда» на главной странице не найдено.")
        if not await self.fill_trip_dates(page):
            raise RuntimeError("Поля дат туда/обратно на главной странице не найдены.")

        clicked = False
        for frame in page.frames:
            clicked = await self.click_first(
                [
                    frame.get_by_role(
                        "button",
                        name=re.compile(r"Найти билеты|Найти|Поиск", re.I),
                    ),
                    frame.get_by_text(
                        re.compile(r"^(Найти билеты|Найти|Поиск)$", re.I),
                        exact=True,
                    ),
                    frame.locator(
                        'button[type="submit"], input[type="submit"]'
                    ).filter(has_text=re.compile(r"Найти|Поиск", re.I)),
                ]
            )
            if clicked:
                break
        if not clicked:
            raise RuntimeError("Кнопка поиска билетов не найдена.")

        await page.wait_for_timeout(7500)
        await self.dismiss_popups(page)
        body = (await page.locator("body").inner_text()).lower()
        if "005" not in body and "searchresults" not in page.url.lower():
            raise RuntimeError("РЖД не открыл результаты поиска заданного маршрута.")

    @staticmethod
    def train_pattern(train: str) -> re.Pattern[str]:
        match = re.match(r"0*(\d+)\s*([A-ZА-Я]?)", train.upper())
        if not match:
            return re.compile(re.escape(train), re.I)
        digits, letter = match.groups()
        suffix = r"[ЭE]" if letter in {"Э", "E"} else re.escape(letter)
        return re.compile(rf"\b0*{re.escape(digits)}\s*{suffix}\b", re.I)

    async def choose_outbound_train(self, page: Page) -> None:
        pattern = self.train_pattern(OUTBOUND["train"])
        card = await self.text_container(page, pattern)
        if card is None:
            raise RuntimeError("Поезд 005Э в результатах поиска не найден.")

        action = re.compile(
            r"Выбрать|Купить билет|Выбрать места|Посмотреть места|Показать места|Места",
            re.I,
        )
        clicked = await self.click_first(
            [
                card.get_by_role("button", name=action),
                card.get_by_role("link", name=action),
                card.get_by_text(action),
            ]
        )
        if not clicked:
            try:
                await card.click()
                clicked = True
            except Exception:
                clicked = False
        if not clicked:
            raise RuntimeError("Не удалось открыть поезд 005Э.")
        await page.wait_for_timeout(3500)

        # Некоторые версии интерфейса сначала раскрывают тарифы. В таком случае
        # отдельно выбираем «Купе»/«места» внутри раскрытой карточки.
        if "/seats" not in page.url.lower():
            expanded_card = await self.text_container(page, pattern)
            card = expanded_card if expanded_card is not None else page.locator("body")
            await self.click_first(
                [
                    card.get_by_role("button", name=re.compile(r"Купе|Выбрать места|Места", re.I)),
                    card.get_by_role("link", name=re.compile(r"Купе|Выбрать места|Места", re.I)),
                    card.get_by_text(re.compile(r"^(Купе|Выбрать места|Места)$", re.I)),
                ]
            )
            await page.wait_for_timeout(3800)

        if "/seats" not in page.url.lower():
            # Прямой URL применяется только после поиска — нужный маршрут уже
            # существует в sessionStorage билетного приложения.
            await self.goto(page, OUTBOUND_SEATS_URL, 4500)

    async def go_to_return_search(self, page: Page) -> None:
        clicked = await self.click_first(
            [
                page.get_by_role(
                    "button",
                    name=re.compile(
                        r"К обратной поездке|Обратная поездка|Выбрать обратный поезд|Продолжить к обратной",
                        re.I,
                    ),
                ),
                page.get_by_role(
                    "link",
                    name=re.compile(
                        r"К обратной поездке|Обратная поездка|Выбрать обратный поезд",
                        re.I,
                    ),
                ),
                page.get_by_text(
                    re.compile(
                        r"^(К обратной поездке|Обратная поездка|Выбрать обратный поезд)$",
                        re.I,
                    ),
                    exact=True,
                ),
            ]
        )
        if clicked:
            await page.wait_for_timeout(5200)
        if "searchresults" not in page.url.lower() or "return" not in page.url.lower():
            await self.goto(page, RETURN_SEARCH_URL, 5200)

    async def text_container(self, page: Page, pattern: re.Pattern[str]) -> Locator | None:
        candidates = [
            page.locator("article").filter(has_text=pattern),
            page.locator("section").filter(has_text=pattern),
            page.locator("li").filter(has_text=pattern),
            page.locator("div").filter(has_text=pattern),
            page.get_by_text(pattern),
        ]
        item = await self.first_visible(candidates)
        if item is None:
            return None
        try:
            tag = (await item.evaluate("el => el.tagName.toLowerCase()")) or ""
            if tag in {"article", "section", "li"}:
                return item
            ancestor = item.locator(
                "xpath=ancestor::*[self::article or self::section or self::li or self::div][1]"
            )
            if await ancestor.count():
                return ancestor.first
        except Exception:
            pass
        return item

    async def select_wagon(self, page: Page, wagon: str) -> None:
        number = wagon.lstrip("0") or "0"
        patterns = [
            re.compile(rf"\bвагон\s*№?\s*0*{re.escape(number)}\b", re.I),
            re.compile(rf"\b№\s*0*{re.escape(number)}\b", re.I),
            re.compile(rf"^\s*0*{re.escape(number)}\s*$", re.I),
        ]
        for pattern in patterns:
            container = await self.text_container(page, pattern)
            if container is None:
                continue
            clicked = await self.click_first(
                [
                    container.get_by_role(
                        "button", name=re.compile(r"Выбрать|Места|Продолжить", re.I)
                    ),
                    container.get_by_role("link", name=re.compile(r"Выбрать|Места", re.I)),
                    container.get_by_text(re.compile(r"Выбрать|Места", re.I)),
                ]
            )
            if not clicked:
                try:
                    await container.click()
                    clicked = True
                except Exception:
                    clicked = False
            if clicked:
                await page.wait_for_timeout(3000)
                return
        raise RuntimeError(f"Вагон {wagon} не найден или недоступен.")

    async def select_seat(self, page: Page, seat: str) -> None:
        normalized = seat.lstrip("0") or "0"
        exact = re.compile(rf"^\s*0*{re.escape(normalized)}\s*$")
        aria = re.compile(
            rf"(место|seat|place)\D*0*{re.escape(normalized)}\b", re.I
        )
        candidates = [
            page.locator(f'[data-seat-number="{seat}"]'),
            page.locator(f'[data-place-number="{seat}"]'),
            page.locator(f'[data-seat="{seat}"]'),
            page.locator(f'[data-seat-number="{normalized}"]'),
            page.get_by_role("button", name=aria),
            page.locator('[role="button"]').filter(has_text=exact),
            page.locator("button").filter(has_text=exact),
            page.get_by_text(exact, exact=True),
        ]
        for locator in candidates:
            try:
                count = min(await locator.count(), 40)
                for index in range(count):
                    item = locator.nth(index)
                    if not await self.visible(item, 650):
                        continue
                    if not await item.is_enabled(timeout=650):
                        continue
                    attrs = " ".join(
                        [
                            (await item.get_attribute("class") or ""),
                            (await item.get_attribute("aria-label") or ""),
                            (await item.get_attribute("title") or ""),
                        ]
                    ).lower()
                    if (await item.get_attribute("aria-disabled") or "").lower() == "true":
                        continue
                    if any(
                        marker in attrs
                        for marker in (
                            "disabled",
                            "occupied",
                            "unavailable",
                            "busy",
                            "sold",
                            "занято",
                            "недоступ",
                        )
                    ):
                        continue
                    await item.scroll_into_view_if_needed()
                    await item.click()
                    await page.wait_for_timeout(1300)
                    return
            except Exception:
                continue
        raise RuntimeError(f"Место {seat} сейчас недоступно.")

    async def select_only_passenger(self, page: Page) -> None:
        full_name = re.compile(
            rf"{PASSENGER['surname']}.*{PASSENGER['name']}|"
            rf"{PASSENGER['name']}.*{PASSENGER['surname']}",
            re.I,
        )
        card = await self.text_container(page, full_name)
        if card is not None:
            checkbox = await self.first_visible(
                [
                    card.get_by_role("checkbox"),
                    card.get_by_role("radio"),
                    card.locator('input[type="checkbox"], input[type="radio"]'),
                ]
            )
            if checkbox is not None:
                try:
                    if not await checkbox.is_checked():
                        await checkbox.click(force=True)
                    return
                except Exception:
                    pass
            try:
                await card.click()
                await page.wait_for_timeout(600)
                return
            except Exception:
                pass

        # Резервный вариант: пользователь сказал, что доступен ровно один пассажир.
        controls = page.locator(
            'input[type="checkbox"]:not([disabled]), input[type="radio"]:not([disabled])'
        )
        count = await controls.count()
        if count == 1:
            only = controls.first
            if not await only.is_checked():
                await only.click(force=True)
            return

        # Иногда сохранённый пассажир уже выбран и элементов управления нет.
        body = (await page.locator("body").inner_text()).lower()
        if PASSENGER["surname"].lower() in body and PASSENGER["name"].lower() in body:
            return
        raise RuntimeError("Единственный сохранённый пассажир не найден.")

    async def continue_to_boarding(self, page: Page, boarding_url: str) -> None:
        clicked = await self.click_first(
            [
                page.get_by_role(
                    "button",
                    name=re.compile(
                        r"Продолжить|Оформить|Перейти к данным|Выбрать пассажира",
                        re.I,
                    ),
                ),
                page.get_by_text(
                    re.compile(r"^(Продолжить|Оформить|Выбрать пассажира)$", re.I),
                    exact=True,
                ),
            ]
        )
        if clicked:
            await page.wait_for_timeout(3200)
        if "/boarding" not in page.url:
            await self.goto(page, boarding_url, 3500)

    async def choose_return_train(self, page: Page) -> None:
        # Ищем карточку, в которой одновременно есть 19:40 и «Купе».
        time_pattern = re.compile(r"\b19\s*[:.]\s*40\b")
        type_pattern = re.compile(r"Купе", re.I)
        candidates = [
            page.locator("article").filter(has_text=time_pattern).filter(has_text=type_pattern),
            page.locator("section").filter(has_text=time_pattern).filter(has_text=type_pattern),
            page.locator("li").filter(has_text=time_pattern).filter(has_text=type_pattern),
            page.locator("div").filter(has_text=time_pattern).filter(has_text=type_pattern),
        ]
        card = await self.first_visible(candidates)
        if card is None:
            raise RuntimeError("Обратный поезд 19:40 с купе не найден.")

        action = re.compile(
            r"Выбрать|Купить билет|Выбрать места|Посмотреть места|Показать места|Места",
            re.I,
        )
        clicked = await self.click_first(
            [
                card.get_by_role("button", name=action),
                card.get_by_role("link", name=action),
                card.get_by_text(action),
            ]
        )
        if not clicked:
            try:
                await card.click()
                clicked = True
            except Exception:
                clicked = False
        if not clicked:
            raise RuntimeError("Не удалось открыть обратный поезд 19:40.")
        await page.wait_for_timeout(4200)

    async def click_order(self, page: Page) -> None:
        clicked = await self.click_first(
            [
                page.get_by_role("button", name=re.compile(r"Оформить заказ", re.I)),
                page.get_by_text(re.compile(r"^ОФОРМИТЬ ЗАКАЗ$", re.I), exact=True),
                page.locator("button").filter(has_text=re.compile(r"Оформить заказ", re.I)),
            ]
        )
        if not clicked:
            raise RuntimeError("Кнопка «ОФОРМИТЬ ЗАКАЗ» не найдена.")
        await page.wait_for_timeout(5000)

    async def verify_order(self, page: Page) -> None:
        body = (await page.locator("body").inner_text()).lower()
        url = page.url.lower()
        success_markers = (
            "оплатить",
            "время на оплату",
            "заказ оформлен",
            "заказ создан",
            "номер заказа",
        )
        if any(marker in body for marker in success_markers) or "payment" in url:
            return
        error_markers = (
            "место уже занято",
            "недоступно для заказа",
            "не удалось оформить",
            "ошибка оформления",
        )
        for marker in error_markers:
            if marker in body:
                raise RuntimeError(marker.capitalize() + ".")
        raise RuntimeError("РЖД не подтвердил создание заказа.")

    async def book_round_trip(self) -> tuple[str, Path | None]:
        page = await self.page()
        stage = "открытие браузера"
        try:
            # Вход делается только при отсутствии активной сессии.
            stage = "авторизация на РЖД"
            await self.login_once(page)

            # Всегда заново задаём маршрут и обе даты на www.rzd.ru. Это создаёт
            # корректный контекст заказа туда-обратно в билетном приложении.
            stage = "заполнение маршрута и дат"
            await self.search_round_trip(page)

            # Туда: выбираем поезд 005Э, затем вагон 5 и место 35.
            stage = "выбор поезда 005Э"
            await self.choose_outbound_train(page)
            stage = "выбор вагона 5 туда"
            await self.select_wagon(page, OUTBOUND["wagon"])
            stage = "выбор места 35 туда"
            await self.select_seat(page, OUTBOUND["seat"])
            stage = "выбор пассажира туда"
            await self.continue_to_boarding(page, OUTBOUND_BOARDING_URL)
            await self.select_only_passenger(page)

            # Переходим штатной кнопкой к выбору обратного поезда. Жёсткий URL
            # используется только как запасной вариант.
            stage = "переход к обратной поездке"
            await self.go_to_return_search(page)

            # Обратная поездка: 29.07, 19:40, купе.
            stage = "выбор обратного поезда 19:40"
            await self.choose_return_train(page)
            if "/seats" not in page.url:
                await self.goto(page, RETURN_SEATS_URL)
            stage = "выбор вагона 7 обратно"
            await self.select_wagon(page, RETURN["wagon"])
            stage = "выбор места 35 обратно"
            await self.select_seat(page, RETURN["seat"])
            stage = "выбор пассажира обратно"
            await self.continue_to_boarding(page, RETURN_BOARDING_URL)
            await self.select_only_passenger(page)

            # Создаём заказ, но оплату не нажимаем.
            stage = "нажатие «ОФОРМИТЬ ЗАКАЗ»"
            await self.click_order(page)
            stage = "проверка создания заказа"
            await self.verify_order(page)
            # Снимок является только дополнением и не влияет на статус заказа.
            shot = await self.screenshot(page, "order_created")
            return (
                "Заказ создан: Владивосток → Хабаровск-1, 25.07 поезд 005Э, "
                "вагон 5, место 35; Хабаровск-1 → Владивосток, "
                "29.07 поезд 006Э 19:40, купе, вагон 7, место 35. "
                "Оплата не выполнялась.",
                shot,
            )
        except PlaywrightTimeoutError as exc:
            shot = await self.screenshot(page, "timeout")
            error = RuntimeError(f"Этап «{stage}»: {friendly_browser_error(exc)}")
            setattr(error, "screenshot", shot)
            raise error from exc
        except Exception as exc:
            shot = await self.screenshot(page, "booking_error")
            original = friendly_browser_error(exc)
            error = RuntimeError(f"Этап «{stage}»: {original}")
            setattr(error, "screenshot", shot)
            raise error from exc
        finally:
            await page.close()


async def run_booking(reason: str = "scheduler") -> None:
    global status
    if service is None:
        return
    if run_lock.locked():
        logger.info("Проверка уже выполняется; повторный запуск пропущен.")
        return

    async with run_lock:
        started = utc_now()
        status.update(
            {
                "state": "running",
                "message": f"Запуск: {reason}",
                "last_started_at": iso(started),
            }
        )
        logger.info("Запущено бронирование (%s).", reason)
        await telegram_text(
            "🚆 РЖД: начинаю повторное оформление заданного заказа туда‑обратно."
        )

        screenshot: Path | None = None
        try:
            message, screenshot = await service.book_round_trip()
            finished = utc_now()
            status.update(
                {
                    "state": "success",
                    "message": message,
                    "last_finished_at": iso(finished),
                    "last_success_at": iso(finished),
                    "next_due_at": (
                        iso(finished + timedelta(hours=CHECK_INTERVAL_HOURS))
                        if status.get("automation_enabled", True)
                        else None
                    ),
                }
            )
            if screenshot is not None:
                status["last_screenshot"] = str(screenshot)
            logger.info(message)
            await telegram_text("✅ РЖД: " + message)
            if screenshot is not None:
                await telegram_photo(screenshot, "РЖД: заказ создан, открыта стадия оплаты.")
            else:
                logger.warning("Заказ подтверждён, но диагностический скриншот не создан.")
        except Exception as exc:
            finished = utc_now()
            # book_round_trip сохраняет диагностический скриншот перед пробросом.
            screenshot = getattr(exc, "screenshot", None)
            message = str(exc) or type(exc).__name__
            status.update(
                {
                    "state": "error",
                    "message": message,
                    "last_finished_at": iso(finished),
                    "next_due_at": (
                        iso(finished + timedelta(hours=CHECK_INTERVAL_HOURS))
                        if status.get("automation_enabled", True)
                        else None
                    ),
                }
            )
            if isinstance(screenshot, Path):
                status["last_screenshot"] = str(screenshot)
            logger.exception("Ошибка бронирования: %s", message)
            await telegram_text("⚠️ РЖД: " + message)
            if isinstance(screenshot, Path):
                await telegram_photo(screenshot, "РЖД: ошибка сценария, последний экран.")
            else:
                logger.warning("Ошибка сценария сохранена без скриншота.")


def is_due() -> bool:
    if not status.get("automation_enabled", True):
        return False
    last_started = status.get("last_started_at")
    if not last_started:
        return True
    try:
        previous = datetime.fromisoformat(last_started)
    except ValueError:
        return True
    return utc_now() - previous >= timedelta(hours=CHECK_INTERVAL_HOURS)




def local_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(ZoneInfo(APP_TIMEZONE)).strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return value


def is_authorized_update(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.id in ALLOWED_TELEGRAM_IDS


async def reject_unauthorized(update: Update) -> None:
    if update.callback_query is not None:
        await update.callback_query.answer("Доступ запрещён.", show_alert=True)
    elif update.effective_message is not None:
        await update.effective_message.reply_text("⛔ У этого Telegram ID нет доступа к боту.")


def control_keyboard() -> InlineKeyboardMarkup:
    enabled = bool(status.get("automation_enabled", True))
    pause_button = (
        InlineKeyboardButton("⏸ Пауза", callback_data="pause")
        if enabled
        else InlineKeyboardButton("▶️ Возобновить", callback_data="resume")
    )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Статус", callback_data="status"),
                InlineKeyboardButton("🚆 Запустить сейчас", callback_data="run"),
            ],
            [pause_button, InlineKeyboardButton("🖼 Скриншот", callback_data="screenshot")],
            [
                InlineKeyboardButton("🧾 Параметры заказа", callback_data="config"),
                InlineKeyboardButton("❓ Помощь", callback_data="help"),
            ],
        ]
    )


def status_text() -> str:
    state_labels = {
        "starting": "запускается",
        "ready": "готов",
        "running": "выполняет оформление",
        "success": "последний запуск успешен",
        "error": "ошибка последнего запуска",
        "paused": "автоматический запуск приостановлен",
    }
    enabled = bool(status.get("automation_enabled", True))
    state = state_labels.get(str(status.get("state")), str(status.get("state")))
    return (
        "📊 Статус РЖД-бота\n\n"
        f"Состояние: {state}\n"
        f"Автозапуск: {'включён' if enabled else 'на паузе'}\n"
        f"Интервал: {CHECK_INTERVAL_HOURS} ч.\n"
        f"Прокси Chromium: {proxy_label()}\n"
        f"Последний старт: {local_time(status.get('last_started_at'))}\n"
        f"Последнее завершение: {local_time(status.get('last_finished_at'))}\n"
        f"Последний успех: {local_time(status.get('last_success_at'))}\n"
        f"Следующий запуск: {local_time(status.get('next_due_at'))}\n\n"
        f"Сообщение: {status.get('message', '—')}"
    )


def config_text() -> str:
    return (
        "🧾 Фиксированный заказ\n\n"
        "Туда: Владивосток → Хабаровск-1\n"
        "Дата: 25.07.2026\n"
        "Поезд: 005Э\n"
        "Вагон: 05\n"
        "Место: 35\n\n"
        "Обратно: Хабаровск-1 → Владивосток\n"
        "Дата: 29.07.2026\n"
        "Поезд: 006Э, отправление 19:40\n"
        "Тип: Купе\n"
        "Вагон: 07\n"
        "Место: 35\n\n"
        f"Пассажир: {PASSENGER['surname']} {PASSENGER['name']} {PASSENGER['patronymic']}\n"
        f"Прокси для РЖД: {proxy_label()}\n"
        "Бот создаёт заказ и останавливается на стадии оплаты."
    )


def help_text() -> str:
    return (
        "🤖 Команды бота\n\n"
        "/start — открыть меню\n"
        "/status — показать состояние\n"
        "/run — запустить оформление сейчас\n"
        "/pause — остановить автоматические запуски\n"
        "/resume — возобновить автоматические запуски\n"
        "/screenshot — последний диагностический экран\n"
        "/config — параметры фиксированного заказа\n"
        "/ping — проверить, что бот жив\n"
        "/help — эта справка\n\n"
        "Автоматический запуск выполняется раз в два часа. Ручной /run доступен и во время паузы."
    )


async def send_menu(update: Update, text: str) -> None:
    if update.callback_query is not None:
        query = update.callback_query
        await query.answer()
        try:
            await query.edit_message_text(text, reply_markup=control_keyboard())
        except Exception:
            if query.message is not None:
                await query.message.reply_text(text, reply_markup=control_keyboard())
    elif update.effective_message is not None:
        await update.effective_message.reply_text(text, reply_markup=control_keyboard())


async def bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized_update(update):
        await reject_unauthorized(update)
        return
    await send_menu(
        update,
        "🚆 РЖД-бот запущен. Он автоматически повторяет заданное оформление раз в "
        f"{CHECK_INTERVAL_HOURS} часа и присылает результат сюда.",
    )


async def bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized_update(update):
        await reject_unauthorized(update)
        return
    await send_menu(update, status_text())


async def bot_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized_update(update):
        await reject_unauthorized(update)
        return
    if run_lock.locked():
        await send_menu(update, "⏳ Оформление уже выполняется. Второй запуск не создан.")
        return
    asyncio.create_task(run_booking("Telegram /run"))
    await send_menu(update, "🚆 Ручной запуск принят. Результат и скриншот придут отдельным сообщением.")


async def pause_automation() -> None:
    status["automation_enabled"] = False
    status["state"] = "paused"
    status["message"] = "Автоматические запуски приостановлены через Telegram."
    status["next_due_at"] = None
    job = scheduler.get_job("rzd_single_order")
    if job is not None:
        scheduler.pause_job("rzd_single_order")


async def resume_automation() -> None:
    status["automation_enabled"] = True
    status["state"] = "ready"
    status["message"] = "Автоматические запуски возобновлены через Telegram."
    next_run = utc_now() + timedelta(hours=CHECK_INTERVAL_HOURS)
    status["next_due_at"] = iso(next_run)
    job = scheduler.get_job("rzd_single_order")
    if job is not None:
        scheduler.resume_job("rzd_single_order")
        scheduler.modify_job("rzd_single_order", next_run_time=next_run)


async def bot_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized_update(update):
        await reject_unauthorized(update)
        return
    await pause_automation()
    await send_menu(update, "⏸ Автоматическое оформление поставлено на паузу. Ручная команда /run продолжает работать.")


async def bot_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized_update(update):
        await reject_unauthorized(update)
        return
    await resume_automation()
    await send_menu(update, f"▶️ Автоматическое оформление возобновлено. Следующий запуск через {CHECK_INTERVAL_HOURS} ч.")


async def send_last_screenshot(update: Update) -> None:
    if not is_authorized_update(update):
        await reject_unauthorized(update)
        return
    raw_path = status.get("last_screenshot")
    path = Path(raw_path) if raw_path else None
    target_message = update.callback_query.message if update.callback_query else update.effective_message
    if update.callback_query is not None:
        await update.callback_query.answer()
    if target_message is None:
        return
    if path is None or not path.exists():
        await target_message.reply_text("🖼 Скриншота пока нет: ещё не было завершённого запуска.", reply_markup=control_keyboard())
        return
    with path.open("rb") as image:
        await target_message.reply_photo(
            photo=image,
            caption="Последний экран сценария РЖД.",
            reply_markup=control_keyboard(),
        )


async def bot_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_last_screenshot(update)


async def bot_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized_update(update):
        await reject_unauthorized(update)
        return
    await send_menu(update, config_text())


async def bot_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized_update(update):
        await reject_unauthorized(update)
        return
    await send_menu(update, help_text())


async def bot_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized_update(update):
        await reject_unauthorized(update)
        return
    await send_menu(update, "🏓 Бот работает. Веб-сервис, планировщик и Telegram polling активны.")


async def bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized_update(update):
        await reject_unauthorized(update)
        return
    query = update.callback_query
    if query is None:
        return
    action = query.data or ""
    handlers = {
        "status": bot_status,
        "run": bot_run,
        "pause": bot_pause,
        "resume": bot_resume,
        "screenshot": bot_screenshot,
        "config": bot_config,
        "help": bot_help,
    }
    handler = handlers.get(action)
    if handler is None:
        await query.answer("Неизвестная команда.")
        return
    await handler(update, context)


async def start_telegram_bot() -> Application:
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", bot_start))
    application.add_handler(CommandHandler("status", bot_status))
    application.add_handler(CommandHandler("run", bot_run))
    application.add_handler(CommandHandler("pause", bot_pause))
    application.add_handler(CommandHandler("resume", bot_resume))
    application.add_handler(CommandHandler("screenshot", bot_screenshot))
    application.add_handler(CommandHandler("config", bot_config))
    application.add_handler(CommandHandler("ping", bot_ping))
    application.add_handler(CommandHandler("help", bot_help))
    application.add_handler(CallbackQueryHandler(bot_callback))

    await application.initialize()
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Открыть меню"),
            BotCommand("status", "Статус автоматизации"),
            BotCommand("run", "Запустить оформление сейчас"),
            BotCommand("pause", "Поставить автозапуск на паузу"),
            BotCommand("resume", "Возобновить автозапуск"),
            BotCommand("screenshot", "Последний скриншот"),
            BotCommand("config", "Параметры заказа"),
            BotCommand("ping", "Проверить бота"),
            BotCommand("help", "Список команд"),
        ]
    )
    await application.start()
    if application.updater is None:
        raise RuntimeError("Telegram updater не создан.")
    await application.updater.start_polling(drop_pending_updates=True)
    return application


async def stop_telegram_bot(application: Application | None) -> None:
    if application is None:
        return
    if application.updater is not None:
        await application.updater.stop()
    await application.stop()
    await application.shutdown()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service, telegram_bot
    require_configuration()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    service = RzdAutomation()
    await service.start()

    job_options: dict[str, Any] = {}
    if RUN_ON_STARTUP:
        job_options["next_run_time"] = utc_now()
    scheduler.add_job(
        run_booking,
        trigger="interval",
        hours=CHECK_INTERVAL_HOURS,
        kwargs={"reason": "interval"},
        id="rzd_single_order",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        **job_options,
    )
    scheduler.start()
    telegram_bot = await start_telegram_bot()
    status.update(
        {
            "state": "ready",
            "message": "Сервис запущен.",
            "next_due_at": iso(
                utc_now()
                if RUN_ON_STARTUP
                else utc_now() + timedelta(hours=CHECK_INTERVAL_HOURS)
            ),
        }
    )
    await telegram_text(
        f"🟢 РЖД-сервис запущен. Один заказ, один пассажир, интервал {CHECK_INTERVAL_HOURS} ч."
    )
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await stop_telegram_bot(telegram_bot)
        telegram_bot = None
        if service is not None:
            await service.close()


app = FastAPI(title="RZD private Telegram booking bot", lifespan=lifespan)


@app.get("/")
async def root(background_tasks: BackgroundTasks) -> JSONResponse:
    # На бесплатном Render внешний ping одновременно будит сервис. Если во время
    # сна интервал был пропущен, первый запрос запускает задачу сразу.
    if is_due() and not run_lock.locked():
        background_tasks.add_task(run_booking, "wake-up request")
    return JSONResponse(
        {
            "service": "rzd-private-telegram-booking",
            "authorized_telegram_users": len(ALLOWED_TELEGRAM_IDS),
            "order": "fixed",
            **status,
        }
    )


@app.get("/health")
async def health(background_tasks: BackgroundTasks) -> JSONResponse:
    if is_due() and not run_lock.locked():
        background_tasks.add_task(run_booking, "health wake-up")
    return JSONResponse({"ok": True, "state": status["state"]})

