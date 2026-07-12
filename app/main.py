from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from cryptography.fernet import Fernet, InvalidToken
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from starlette.middleware.sessions import SessionMiddleware


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "rzd.sqlite3"
STATE_PATH = DATA_DIR / "session" / "storage_state.json"
SCREENSHOT_DIR = DATA_DIR / "screenshots"

APP_SECRET = os.getenv("APP_SECRET", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
CHECK_INTERVAL_HOURS = max(1, int(os.getenv("CHECK_INTERVAL_HOURS", "2")))
HEADLESS = os.getenv("HEADLESS", "true").lower() in {"1", "true", "yes", "on"}
AUTO_RESERVE = os.getenv("AUTO_RESERVE", "true").lower() in {"1", "true", "yes", "on"}

RZD_HOME = "https://ticket.rzd.ru/"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("rzd-web")

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
scheduler = AsyncIOScheduler(timezone="UTC")
browser_service: "RzdBrowserService | None" = None
job_lock = asyncio.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_config() -> None:
    if not APP_SECRET:
        raise RuntimeError("APP_SECRET is required.")
    if not ADMIN_PASSWORD:
        logger.warning("ADMIN_PASSWORD is empty: dashboard login is disabled until it is set.")


class SecretStore:
    def __init__(self, secret: str):
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        self.fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self.fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return ""


secret_store = SecretStore(APP_SECRET or "development-only")
signer = URLSafeSerializer(APP_SECRET or "development-only", salt="rzd-admin")


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    with db() as con:
        con.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                from_name TEXT NOT NULL,
                to_name TEXT NOT NULL,
                train TEXT NOT NULL,
                wagon TEXT NOT NULL,
                seat TEXT NOT NULL,
                search_url TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'new',
                last_message TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT,
                reserved_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                trip_id INTEGER,
                screenshot TEXT
            );
            """
        )


def set_setting(key: str, value: str, encrypted: bool = True) -> None:
    stored = secret_store.encrypt(value) if encrypted else value
    with db() as con:
        con.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, stored),
        )


def get_setting(key: str, default: str = "", encrypted: bool = True) -> str:
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    value = row["value"]
    return secret_store.decrypt(value) if encrypted else value


def log_event(
    level: str,
    message: str,
    trip_id: Optional[int] = None,
    screenshot: Optional[str] = None,
) -> None:
    logger.log(getattr(logging, level.upper(), logging.INFO), message)
    with db() as con:
        con.execute(
            "INSERT INTO logs(created_at, level, message, trip_id, screenshot) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), level.upper(), message, trip_id, screenshot),
        )
        con.execute(
            "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 200)"
        )


def get_trips(enabled_only: bool = False) -> list[sqlite3.Row]:
    query = "SELECT * FROM trips"
    if enabled_only:
        query += " WHERE enabled=1"
    query += " ORDER BY date, id"
    with db() as con:
        return list(con.execute(query).fetchall())


def get_trip(trip_id: int) -> sqlite3.Row:
    with db() as con:
        row = con.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not row:
        raise KeyError(trip_id)
    return row


def update_trip_result(
    trip_id: int,
    status: str,
    message: str,
    *,
    disable: bool = False,
    reserved: bool = False,
) -> None:
    with db() as con:
        con.execute(
            """
            UPDATE trips
            SET status=?,
                last_message=?,
                last_checked_at=?,
                enabled=CASE WHEN ? THEN 0 ELSE enabled END,
                reserved_at=CASE WHEN ? THEN ? ELSE reserved_at END
            WHERE id=?
            """,
            (
                status,
                message,
                now_iso(),
                int(disable),
                int(reserved),
                now_iso(),
                trip_id,
            ),
        )


def public_settings() -> dict[str, str]:
    keys = [
        "rzd_login",
        "passenger_surname",
        "passenger_name",
        "passenger_patronymic",
        "passenger_birth_date",
        "passenger_document_type",
        "passenger_document_number",
        "passenger_phone",
        "passenger_email",
        "telegram_chat_id",
    ]
    result = {key: get_setting(key) for key in keys}
    result["has_rzd_password"] = "yes" if get_setting("rzd_password") else ""
    result["has_telegram_token"] = "yes" if get_setting("telegram_token") else ""
    return result


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def verify_csrf(request: Request, supplied: str) -> None:
    expected = request.session.get("csrf", "")
    if not expected or not hmac.compare_digest(expected, supplied or ""):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def is_admin(request: Request) -> bool:
    token = request.session.get("admin")
    if not token:
        return False
    try:
        return signer.loads(token) == "authenticated"
    except BadSignature:
        return False


def admin_required(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="Authentication required")


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def safe_search_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"ticket.rzd.ru", "www.rzd.ru", "pass.rzd.ru"}:
        raise ValueError("Разрешены только HTTPS-ссылки официальных доменов РЖД.")
    return url


async def send_telegram(message: str) -> None:
    token = get_setting("telegram_token")
    chat_id = get_setting("telegram_chat_id")
    if not token or not chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": message},
            )
            response.raise_for_status()
    except Exception as exc:
        log_event("WARNING", f"Telegram notification failed: {type(exc).__name__}")


class RzdBrowserService:
    def __init__(self) -> None:
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.login_page: Page | None = None
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        async with self.lock:
            if self.context:
                return
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=HEADLESS,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            state = str(STATE_PATH) if STATE_PATH.exists() else None
            self.context = await self.browser.new_context(
                storage_state=state,
                locale="ru-RU",
                timezone_id=os.getenv("APP_TIMEZONE", "Asia/Vladivostok"),
                viewport={"width": 1440, "height": 1000},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36"
                ),
            )
            self.context.set_default_timeout(12_000)

    async def close(self) -> None:
        async with self.lock:
            if self.context:
                await self.context.close()
                self.context = None
            if self.browser:
                await self.browser.close()
                self.browser = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None

    async def reload_context(self) -> None:
        await self.close()
        await self.start()

    async def save_state(self) -> None:
        if not self.context:
            return
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        await self.context.storage_state(path=str(STATE_PATH))

    async def new_page(self) -> Page:
        # Most callers already hold self.lock. Calling start() unconditionally
        # here would try to acquire the same non-reentrant asyncio.Lock again.
        if self.context is None:
            await self.start()
        assert self.context is not None
        return await self.context.new_page()

    async def screenshot(self, page: Page, prefix: str) -> str:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Zа-яА-Я0-9_-]", "_", prefix)[:80]
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe}.png"
        path = SCREENSHOT_DIR / filename
        await page.screenshot(path=str(path), full_page=True)
        return filename

    async def first_visible(self, locators: list[Locator]) -> Locator | None:
        for locator in locators:
            try:
                count = min(await locator.count(), 12)
                for index in range(count):
                    item = locator.nth(index)
                    if await item.is_visible(timeout=700):
                        return item
            except Exception:
                continue
        return None

    async def click_first(self, locators: list[Locator]) -> bool:
        item = await self.first_visible(locators)
        if item is None:
            return False
        try:
            if not await item.is_enabled(timeout=700):
                return False
            await item.scroll_into_view_if_needed()
            await item.click()
            return True
        except Exception:
            return False

    async def fill_first(self, locators: list[Locator], value: str) -> bool:
        if not value:
            return False
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
        pattern = re.compile(r"^(Принять|Согласен|Хорошо|Понятно|Закрыть)$", re.I)
        await self.click_first(
            [
                page.get_by_role("button", name=pattern),
                page.get_by_text(pattern, exact=True),
            ]
        )

    async def is_logged_in(self, page: Page) -> bool:
        positive = [
            page.get_by_text(re.compile(r"Мои заказы|Мои поездки|Выйти", re.I)),
            page.get_by_role("link", name=re.compile(r"Личный кабинет|Профиль", re.I)),
        ]
        if await self.first_visible(positive):
            return True

        # Some versions only show an avatar/menu after login.
        avatar = page.locator(
            '[aria-label*="профил" i], [aria-label*="личн" i], '
            '[data-testid*="profile" i], [class*="profile" i]'
        )
        return await self.first_visible([avatar]) is not None

    async def login_status(self) -> dict[str, Any]:
        async with self.lock:
            page = await self.new_page()
            try:
                await page.goto(RZD_HOME, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(2500)
                return {"logged_in": await self.is_logged_in(page)}
            except Exception:
                return {"logged_in": False}
            finally:
                await page.close()

    async def start_login(self, username: str, password: str) -> dict[str, Any]:
        async with self.lock:
            page = await self.new_page()
            self.login_page = page
            try:
                await page.goto(RZD_HOME, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(2500)
                await self.dismiss_popups(page)

                if await self.is_logged_in(page):
                    await self.save_state()
                    return {"status": "ok", "message": "Сессия РЖД уже авторизована."}

                await self.click_first(
                    [
                        page.get_by_role("button", name=re.compile(r"Войти", re.I)),
                        page.get_by_role("link", name=re.compile(r"Войти", re.I)),
                        page.get_by_text(re.compile(r"^Войти$", re.I), exact=True),
                    ]
                )
                await page.wait_for_timeout(1800)

                login_ok = await self.fill_first(
                    [
                        page.get_by_label(re.compile(r"Логин|Телефон|E-mail|Почта", re.I)),
                        page.locator(
                            'input[name*="login" i], input[name*="username" i], '
                            'input[type="email"], input[type="tel"]'
                        ),
                    ],
                    username,
                )
                password_ok = await self.fill_first(
                    [
                        page.get_by_label(re.compile(r"Пароль", re.I)),
                        page.locator('input[type="password"]'),
                    ],
                    password,
                )
                if not login_ok or not password_ok:
                    screenshot = await self.screenshot(page, "login_fields_not_found")
                    return {
                        "status": "error",
                        "message": "Не удалось найти поля входа. Нужен импорт локальной сессии.",
                        "screenshot": screenshot,
                    }

                await self.click_first(
                    [
                        page.get_by_role("button", name=re.compile(r"Войти|Продолжить", re.I)),
                        page.locator('button[type="submit"]'),
                    ]
                )
                await page.wait_for_timeout(3500)

                if await self.is_logged_in(page):
                    await self.save_state()
                    return {"status": "ok", "message": "Вход в РЖД выполнен."}

                challenge = await self.first_visible(
                    [
                        page.get_by_label(re.compile(r"Код|Captcha|Капча|SMS", re.I)),
                        page.locator(
                            'input[name*="captcha" i], input[placeholder*="код" i], '
                            'input[name*="code" i]'
                        ),
                    ]
                )
                screenshot = await self.screenshot(page, "rzd_login_challenge")
                if challenge:
                    return {
                        "status": "challenge",
                        "message": "РЖД запросил код/CAPTCHA. Введите его по скриншоту.",
                        "screenshot": screenshot,
                    }

                return {
                    "status": "error",
                    "message": "РЖД не подтвердил вход. Проверьте скриншот или импортируйте локальную сессию.",
                    "screenshot": screenshot,
                }
            except Exception as exc:
                screenshot = None
                try:
                    screenshot = await self.screenshot(page, "rzd_login_error")
                except Exception:
                    pass
                return {
                    "status": "error",
                    "message": f"Ошибка входа: {type(exc).__name__}",
                    "screenshot": screenshot,
                }

    async def submit_challenge(self, code: str) -> dict[str, Any]:
        async with self.lock:
            page = self.login_page
            if page is None or page.is_closed():
                return {
                    "status": "error",
                    "message": "Сеанс ввода кода потерян. Запустите вход ещё раз.",
                }

            challenge = await self.first_visible(
                [
                    page.get_by_label(re.compile(r"Код|Captcha|Капча|SMS", re.I)),
                    page.locator(
                        'input[name*="captcha" i], input[placeholder*="код" i], '
                        'input[name*="code" i]'
                    ),
                ]
            )
            if challenge is None:
                return {"status": "error", "message": "Поле кода не найдено."}

            await challenge.fill(code)
            await self.click_first(
                [
                    page.get_by_role("button", name=re.compile(r"Войти|Продолжить|Подтвердить", re.I)),
                    page.locator('button[type="submit"]'),
                ]
            )
            await page.wait_for_timeout(3500)

            if await self.is_logged_in(page):
                await self.save_state()
                await page.close()
                self.login_page = None
                return {"status": "ok", "message": "Вход в РЖД выполнен."}

            screenshot = await self.screenshot(page, "rzd_login_challenge_retry")
            return {
                "status": "challenge",
                "message": "Вход не подтверждён. Возможно, код неверный или нужен новый.",
                "screenshot": screenshot,
            }

    async def fill_station(self, page: Page, direction: str, value: str) -> bool:
        if direction == "from":
            labels = re.compile(r"Откуда|Станция отправления", re.I)
            placeholder = 'input[placeholder*="Откуда" i], input[placeholder*="отправ" i]'
        else:
            labels = re.compile(r"Куда|Станция прибытия", re.I)
            placeholder = 'input[placeholder*="Куда" i], input[placeholder*="прибыт" i]'

        ok = await self.fill_first(
            [
                page.get_by_label(labels),
                page.get_by_role("textbox", name=labels),
                page.locator(placeholder),
            ],
            value,
        )
        if not ok:
            return False
        await page.wait_for_timeout(800)
        await self.click_first(
            [
                page.get_by_role("option", name=re.compile(re.escape(value), re.I)),
                page.locator('[role="listbox"] *').filter(
                    has_text=re.compile(re.escape(value), re.I)
                ),
            ]
        )
        return True

    async def navigate_search(self, page: Page, trip: sqlite3.Row) -> None:
        if trip["search_url"]:
            await page.goto(trip["search_url"], wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(4500)
            return

        await page.goto(RZD_HOME, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(2500)
        await self.dismiss_popups(page)

        if not await self.fill_station(page, "from", trip["from_name"]):
            raise RuntimeError("Поле «Откуда» не найдено.")
        if not await self.fill_station(page, "to", trip["to_name"]):
            raise RuntimeError("Поле «Куда» не найдено.")

        date_ok = await self.fill_first(
            [
                page.get_by_label(re.compile(r"Дата отправления|Дата", re.I)),
                page.locator('input[placeholder*="дд.мм" i], input[placeholder*="Дата" i]'),
            ],
            trip["date"],
        )
        if not date_ok:
            raise RuntimeError("Поле даты не найдено.")

        if not await self.click_first(
            [
                page.get_by_role("button", name=re.compile(r"Найти|Поиск", re.I)),
                page.get_by_text(re.compile(r"^Найти$", re.I), exact=True),
            ]
        ):
            raise RuntimeError("Кнопка поиска не найдена.")

        await page.wait_for_timeout(6000)

    @staticmethod
    def train_pattern(train: str) -> re.Pattern[str]:
        match = re.match(r"0*(\d+)\s*([A-ZА-Я]?)", train.upper())
        if not match:
            return re.compile(re.escape(train), re.I)
        digits, letter = match.groups()
        if letter in {"Э", "E"}:
            suffix = r"[ЭE]"
        else:
            suffix = re.escape(letter)
        return re.compile(rf"\b0*{re.escape(digits)}\s*{suffix}\b", re.I)

    async def container_for_text(self, page: Page, pattern: re.Pattern[str]) -> Locator | None:
        candidates = [
            page.locator("article").filter(has_text=pattern),
            page.locator("section").filter(has_text=pattern),
            page.locator("li").filter(has_text=pattern),
            page.get_by_text(pattern),
        ]
        item = await self.first_visible(candidates)
        if item is None:
            return None

        try:
            tag = await item.evaluate("(el) => el.tagName.toLowerCase()")
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

    async def open_train(self, page: Page, trip: sqlite3.Row) -> bool:
        container = await self.container_for_text(page, self.train_pattern(trip["train"]))
        if container is None:
            return False
        action = re.compile(
            r"Выбрать|Купить билет|Выбрать места|Посмотреть места|Показать места|Места",
            re.I,
        )
        if await self.click_first(
            [
                container.get_by_role("button", name=action),
                container.get_by_role("link", name=action),
                container.get_by_text(action),
            ]
        ):
            await page.wait_for_timeout(4000)
            return True
        try:
            await container.click()
            await page.wait_for_timeout(4000)
            return True
        except Exception:
            return False

    async def open_wagon(self, page: Page, wagon: str) -> bool:
        number = wagon.lstrip("0") or "0"
        patterns = [
            re.compile(rf"\bвагон\s*№?\s*0*{re.escape(number)}\b", re.I),
            re.compile(rf"\b№\s*0*{re.escape(number)}\b", re.I),
            re.compile(rf"^\s*0*{re.escape(number)}\s*$", re.I),
        ]
        for pattern in patterns:
            container = await self.container_for_text(page, pattern)
            if container is None:
                continue
            if await self.click_first(
                [
                    container.get_by_role(
                        "button",
                        name=re.compile(r"Выбрать|Места|Продолжить", re.I),
                    ),
                    container.get_by_text(re.compile(r"Выбрать|Места", re.I)),
                ]
            ):
                await page.wait_for_timeout(3500)
                return True
            try:
                await container.click()
                await page.wait_for_timeout(3500)
                return True
            except Exception:
                continue
        return False

    async def available_seat(self, page: Page, seat: str) -> Locator | None:
        exact = re.compile(rf"^\s*0*{re.escape(seat.lstrip('0') or '0')}\s*$")
        aria = re.compile(rf"(место|seat|place)\D*0*{re.escape(seat.lstrip('0') or '0')}\b", re.I)
        candidates = [
            page.locator(f'[data-seat-number="{seat}"]'),
            page.locator(f'[data-place-number="{seat}"]'),
            page.locator(f'[data-seat="{seat}"]'),
            page.get_by_role("button", name=aria),
            page.locator('[role="button"]').filter(has_text=exact),
            page.locator("button").filter(has_text=exact),
            page.get_by_text(exact, exact=True),
        ]

        for locator in candidates:
            try:
                count = min(await locator.count(), 30)
                for index in range(count):
                    item = locator.nth(index)
                    if not await item.is_visible(timeout=600):
                        continue
                    if not await item.is_enabled(timeout=600):
                        continue
                    aria_disabled = (await item.get_attribute("aria-disabled") or "").lower()
                    classes = (await item.get_attribute("class") or "").lower()
                    if aria_disabled == "true":
                        continue
                    if any(word in classes for word in ("disabled", "occupied", "busy", "sold")):
                        continue
                    return item
            except Exception:
                continue
        return None

    async def fill_passenger(self, page: Page) -> None:
        values = {
            "surname": get_setting("passenger_surname"),
            "name": get_setting("passenger_name"),
            "patronymic": get_setting("passenger_patronymic"),
            "birth": get_setting("passenger_birth_date"),
            "document": get_setting("passenger_document_number"),
            "phone": get_setting("passenger_phone"),
            "email": get_setting("passenger_email"),
        }

        await self.fill_first(
            [page.get_by_label(re.compile(r"Фамилия", re.I)), page.locator('input[placeholder*="Фамилия" i]')],
            values["surname"],
        )
        await self.fill_first(
            [page.get_by_label(re.compile(r"^Имя", re.I)), page.locator('input[placeholder*="Имя" i]')],
            values["name"],
        )
        await self.fill_first(
            [page.get_by_label(re.compile(r"Отчество", re.I)), page.locator('input[placeholder*="Отчество" i]')],
            values["patronymic"],
        )
        await self.fill_first(
            [page.get_by_label(re.compile(r"Дата рождения", re.I)), page.locator('input[placeholder*="дд.мм" i]')],
            values["birth"],
        )
        await self.fill_first(
            [
                page.get_by_label(re.compile(r"Номер документа|Серия и номер", re.I)),
                page.locator('input[placeholder*="документ" i], input[placeholder*="Серия" i]'),
            ],
            values["document"],
        )
        await self.fill_first(
            [page.get_by_label(re.compile(r"Телефон", re.I)), page.locator('input[type="tel"]')],
            values["phone"],
        )
        await self.fill_first(
            [
                page.get_by_label(re.compile(r"E-mail|Email|Электронная почта", re.I)),
                page.locator('input[type="email"]'),
            ],
            values["email"],
        )

    async def reserve(self, trip: sqlite3.Row) -> dict[str, Any]:
        async with self.lock:
            page = await self.new_page()
            try:
                await self.navigate_search(page, trip)

                if not await self.is_logged_in(page):
                    screenshot = await self.screenshot(page, f"trip_{trip['id']}_not_logged_in")
                    return {
                        "status": "login_required",
                        "message": "Сессия РЖД не авторизована.",
                        "screenshot": screenshot,
                    }

                if not await self.open_train(page, trip):
                    screenshot = await self.screenshot(page, f"trip_{trip['id']}_train_not_found")
                    return {
                        "status": "not_found",
                        "message": f"Поезд {trip['train']} не найден в результатах.",
                        "screenshot": screenshot,
                    }

                if not await self.open_wagon(page, trip["wagon"]):
                    screenshot = await self.screenshot(page, f"trip_{trip['id']}_wagon_not_found")
                    return {
                        "status": "not_found",
                        "message": f"Вагон {trip['wagon']} не найден или недоступен.",
                        "screenshot": screenshot,
                    }

                seat = await self.available_seat(page, trip["seat"])
                if seat is None:
                    return {
                        "status": "unavailable",
                        "message": f"Место {trip['seat']} сейчас недоступно.",
                    }

                if not AUTO_RESERVE:
                    screenshot = await self.screenshot(page, f"trip_{trip['id']}_seat_available")
                    return {
                        "status": "available",
                        "message": f"Место {trip['seat']} доступно. Автобронирование отключено.",
                        "screenshot": screenshot,
                    }

                await seat.scroll_into_view_if_needed()
                await seat.click()
                await page.wait_for_timeout(1500)

                # Move from seat map to passenger form.
                await self.click_first(
                    [
                        page.get_by_role(
                            "button",
                            name=re.compile(r"Продолжить|Оформить|Перейти к данным", re.I),
                        ),
                        page.get_by_text(re.compile(r"^(Продолжить|Оформить)$", re.I), exact=True),
                    ]
                )
                await page.wait_for_timeout(2500)
                await self.fill_passenger(page)

                # Try to create the order, but never click the payment button.
                for _ in range(4):
                    body = (await page.locator("body").inner_text()).lower()
                    url = page.url.lower()
                    if (
                        "оплатить" in body
                        or "время на оплату" in body
                        or "заказ создан" in body
                        or "payment" in url
                    ):
                        screenshot = await self.screenshot(page, f"trip_{trip['id']}_reserved")
                        await self.save_state()
                        return {
                            "status": "reserved",
                            "message": (
                                f"Место {trip['seat']} выбрано, заказ дошёл до этапа оплаты. "
                                "Поездка поставлена на паузу."
                            ),
                            "screenshot": screenshot,
                        }

                    clicked = await self.click_first(
                        [
                            page.get_by_role(
                                "button",
                                name=re.compile(
                                    r"Продолжить|Оформить заказ|Подтвердить|Перейти к оплате",
                                    re.I,
                                ),
                            )
                        ]
                    )
                    if not clicked:
                        break
                    await page.wait_for_timeout(2500)

                screenshot = await self.screenshot(page, f"trip_{trip['id']}_attention")
                await self.save_state()
                return {
                    "status": "attention",
                    "message": (
                        f"Место {trip['seat']} выбрано, но создание заказа не подтверждено. "
                        "Проверьте скриншот."
                    ),
                    "screenshot": screenshot,
                }

            except PlaywrightTimeoutError:
                screenshot = await self.screenshot(page, f"trip_{trip['id']}_timeout")
                return {
                    "status": "error",
                    "message": "РЖД не ответил вовремя.",
                    "screenshot": screenshot,
                }
            except Exception as exc:
                screenshot = None
                try:
                    screenshot = await self.screenshot(page, f"trip_{trip['id']}_error")
                except Exception:
                    pass
                return {
                    "status": "error",
                    "message": f"Ошибка браузера: {type(exc).__name__}",
                    "screenshot": screenshot,
                }
            finally:
                await page.close()


async def run_trip(trip_id: int) -> None:
    global browser_service
    if browser_service is None:
        return

    async with job_lock:
        try:
            trip = get_trip(trip_id)
        except KeyError:
            return

        update_trip_result(trip_id, "checking", "Проверка запущена.")
        log_event("INFO", f"Проверка поездки #{trip_id} запущена.", trip_id)

        result = await browser_service.reserve(trip)
        status = result["status"]
        message = result["message"]
        screenshot = result.get("screenshot")
        reserved = status == "reserved"

        update_trip_result(
            trip_id,
            status,
            message,
            disable=reserved,
            reserved=reserved,
        )
        log_event(
            "INFO" if status not in {"error", "login_required"} else "WARNING",
            message,
            trip_id,
            screenshot,
        )

        if status in {"reserved", "available", "attention", "login_required"}:
            await send_telegram(
                f"РЖД: {message}\n"
                f"{trip['date']} — {trip['from_name']} → {trip['to_name']}, "
                f"поезд {trip['train']}, вагон {trip['wagon']}, место {trip['seat']}."
            )


async def run_all_enabled() -> None:
    for trip in get_trips(enabled_only=True):
        await run_trip(trip["id"])


async def scheduled_run() -> None:
    log_event("INFO", "Плановая проверка всех включённых поездок.")
    await run_all_enabled()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global browser_service
    require_config()
    init_db()
    browser_service = RzdBrowserService()
    await browser_service.start()

    scheduler.add_job(
        scheduled_run,
        "interval",
        hours=CHECK_INTERVAL_HOURS,
        id="rzd_periodic_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log_event("INFO", f"Сервис запущен. Интервал проверки: {CHECK_INTERVAL_HOURS} ч.")

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        if browser_service:
            await browser_service.close()


app = FastAPI(title="RZD Seat Assistant", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=APP_SECRET or "development-only",
    same_site="lax",
    https_only=os.getenv("COOKIE_SECURE", "true").lower() in {"1", "true", "yes"},
    max_age=60 * 60 * 24 * 14,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_admin(request):
        return redirect("/")
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "csrf": csrf_token(request),
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(...),
    csrf: str = Form(...),
):
    verify_csrf(request, csrf)
    if not ADMIN_PASSWORD or not hmac.compare_digest(password, ADMIN_PASSWORD):
        return redirect("/login?error=Неверный+пароль")
    request.session["admin"] = signer.dumps("authenticated")
    return redirect("/")


@app.post("/logout")
async def logout(request: Request, csrf: str = Form(...)):
    admin_required(request)
    verify_csrf(request, csrf)
    request.session.clear()
    return redirect("/login")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not is_admin(request):
        return redirect("/login")

    with db() as con:
        logs = list(
            con.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT 40"
            ).fetchall()
        )

    next_run = None
    job = scheduler.get_job("rzd_periodic_check")
    if job and job.next_run_time:
        next_run = job.next_run_time.isoformat(timespec="minutes")

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "csrf": csrf_token(request),
            "trips": get_trips(),
            "logs": logs,
            "next_run": next_run,
            "login_status": get_setting("rzd_login_status", "unknown", encrypted=False),
            "login_message": get_setting("rzd_login_message", "", encrypted=False),
            "challenge_screenshot": get_setting(
                "rzd_challenge_screenshot", "", encrypted=False
            ),
            "interval": CHECK_INTERVAL_HOURS,
            "auto_reserve": AUTO_RESERVE,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not is_admin(request):
        return redirect("/login")
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "csrf": csrf_token(request),
            "settings": public_settings(),
            "has_session": STATE_PATH.exists(),
        },
    )


@app.post("/settings")
async def settings_save(
    request: Request,
    csrf: str = Form(...),
    rzd_login: str = Form(""),
    rzd_password: str = Form(""),
    passenger_surname: str = Form(""),
    passenger_name: str = Form(""),
    passenger_patronymic: str = Form(""),
    passenger_birth_date: str = Form(""),
    passenger_document_type: str = Form("Паспорт РФ"),
    passenger_document_number: str = Form(""),
    passenger_phone: str = Form(""),
    passenger_email: str = Form(""),
    telegram_token: str = Form(""),
    telegram_chat_id: str = Form(""),
):
    admin_required(request)
    verify_csrf(request, csrf)

    values = {
        "rzd_login": rzd_login,
        "passenger_surname": passenger_surname,
        "passenger_name": passenger_name,
        "passenger_patronymic": passenger_patronymic,
        "passenger_birth_date": passenger_birth_date,
        "passenger_document_type": passenger_document_type,
        "passenger_document_number": passenger_document_number,
        "passenger_phone": passenger_phone,
        "passenger_email": passenger_email,
        "telegram_chat_id": telegram_chat_id,
    }
    for key, value in values.items():
        set_setting(key, value.strip())

    if rzd_password.strip():
        set_setting("rzd_password", rzd_password.strip())
    if telegram_token.strip():
        set_setting("telegram_token", telegram_token.strip())

    log_event("INFO", "Настройки обновлены.")
    return redirect("/settings")


@app.post("/session/upload")
async def upload_session(
    request: Request,
    csrf: str = Form(...),
    session_file: UploadFile = File(...),
):
    global browser_service
    admin_required(request)
    verify_csrf(request, csrf)

    raw = await session_file.read()
    if len(raw) > 2_000_000:
        raise HTTPException(status_code=413, detail="Session file is too large")

    try:
        state = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    if not isinstance(state, dict) or "cookies" not in state or "origins" not in state:
        raise HTTPException(status_code=400, detail="Not a Playwright storage_state file")

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if browser_service:
        await browser_service.reload_context()

    set_setting("rzd_login_status", "session_imported", encrypted=False)
    set_setting(
        "rzd_login_message",
        "Сессия импортирована. Нажмите «Проверить вход».",
        encrypted=False,
    )
    log_event("INFO", "Импортирована локальная сессия РЖД.")
    return redirect("/settings")


@app.post("/rzd/login")
async def rzd_login(request: Request, csrf: str = Form(...)):
    global browser_service
    admin_required(request)
    verify_csrf(request, csrf)

    username = get_setting("rzd_login")
    password = get_setting("rzd_password")
    if not username or not password:
        set_setting("rzd_login_status", "error", encrypted=False)
        set_setting(
            "rzd_login_message",
            "Сначала сохраните логин и пароль РЖД в настройках.",
            encrypted=False,
        )
        return redirect("/")

    assert browser_service is not None
    result = await browser_service.start_login(username, password)
    set_setting("rzd_login_status", result["status"], encrypted=False)
    set_setting("rzd_login_message", result["message"], encrypted=False)
    set_setting(
        "rzd_challenge_screenshot",
        result.get("screenshot", ""),
        encrypted=False,
    )
    log_event("INFO", result["message"], screenshot=result.get("screenshot"))
    return redirect("/")


@app.post("/rzd/challenge")
async def rzd_challenge(
    request: Request,
    code: str = Form(...),
    csrf: str = Form(...),
):
    global browser_service
    admin_required(request)
    verify_csrf(request, csrf)
    assert browser_service is not None

    result = await browser_service.submit_challenge(code.strip())
    set_setting("rzd_login_status", result["status"], encrypted=False)
    set_setting("rzd_login_message", result["message"], encrypted=False)
    set_setting(
        "rzd_challenge_screenshot",
        result.get("screenshot", ""),
        encrypted=False,
    )
    log_event("INFO", result["message"], screenshot=result.get("screenshot"))
    return redirect("/")


@app.post("/rzd/status")
async def rzd_status(request: Request, csrf: str = Form(...)):
    global browser_service
    admin_required(request)
    verify_csrf(request, csrf)
    assert browser_service is not None

    result = await browser_service.login_status()
    status = "ok" if result["logged_in"] else "login_required"
    message = "Сессия РЖД активна." if result["logged_in"] else "Сессия РЖД не авторизована."
    set_setting("rzd_login_status", status, encrypted=False)
    set_setting("rzd_login_message", message, encrypted=False)
    log_event("INFO", message)
    return redirect("/")


@app.post("/trips/add")
async def trip_add(
    request: Request,
    csrf: str = Form(...),
    date: str = Form(...),
    from_name: str = Form(...),
    to_name: str = Form(...),
    train: str = Form(...),
    wagon: str = Form(...),
    seat: str = Form(...),
    search_url: str = Form(""),
):
    admin_required(request)
    verify_csrf(request, csrf)

    try:
        safe_url = safe_search_url(search_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with db() as con:
        con.execute(
            """
            INSERT INTO trips(
                date, from_name, to_name, train, wagon, seat,
                search_url, enabled, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'new', ?)
            """,
            (
                date.strip(),
                from_name.strip(),
                to_name.strip(),
                train.strip().upper(),
                wagon.strip(),
                seat.strip(),
                safe_url,
                now_iso(),
            ),
        )
    log_event("INFO", "Добавлена новая поездка.")
    return redirect("/")


@app.post("/trips/{trip_id}/toggle")
async def trip_toggle(request: Request, trip_id: int, csrf: str = Form(...)):
    admin_required(request)
    verify_csrf(request, csrf)
    with db() as con:
        con.execute(
            """
            UPDATE trips
            SET enabled=CASE WHEN enabled=1 THEN 0 ELSE 1 END,
                status=CASE WHEN enabled=1 THEN status ELSE 'enabled' END
            WHERE id=?
            """,
            (trip_id,),
        )
    return redirect("/")


@app.post("/trips/{trip_id}/delete")
async def trip_delete(request: Request, trip_id: int, csrf: str = Form(...)):
    admin_required(request)
    verify_csrf(request, csrf)
    with db() as con:
        con.execute("DELETE FROM trips WHERE id=?", (trip_id,))
    log_event("INFO", f"Поездка #{trip_id} удалена.")
    return redirect("/")


@app.post("/trips/{trip_id}/run")
async def trip_run(
    request: Request,
    trip_id: int,
    background_tasks: BackgroundTasks,
    csrf: str = Form(...),
):
    admin_required(request)
    verify_csrf(request, csrf)
    background_tasks.add_task(run_trip, trip_id)
    return redirect("/")


@app.post("/run-all")
async def run_all(
    request: Request,
    background_tasks: BackgroundTasks,
    csrf: str = Form(...),
):
    admin_required(request)
    verify_csrf(request, csrf)
    background_tasks.add_task(run_all_enabled)
    return redirect("/")


@app.get("/screenshots/{filename}")
async def screenshot_file(request: Request, filename: str):
    admin_required(request)
    safe_name = Path(filename).name
    path = SCREENSHOT_DIR / safe_name
    if not path.exists() or path.suffix.lower() != ".png":
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/png")
