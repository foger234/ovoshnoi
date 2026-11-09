import html
import logging
import os
import re
import time
from pathlib import Path

import aiohttp
from aiohttp import web

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
# Для CORS: "*" или список через запятую, например "https://myshop.ru,https://www.myshop.ru"
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

PHONE_RE = re.compile(r"^\+7 \(\d{3}\) \d{3}-\d{2}-\d{2}$")
LIMITS = {"name": 80, "phone": 20, "address": 250, "delivery_time": 60, "items": 2000, "comment": 1000}
RATE_LIMIT = 5          # заказов
RATE_WINDOW = 600       # за 10 минут с одного IP
_hits: dict[str, list[float]] = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("orders")


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    origin = request.headers.get("Origin", "")
    if "*" in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = "*"
    elif origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    return fwd.split(",")[0].strip() if fwd else (request.remote or "unknown")


def rate_limited(ip: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
    if len(recent) >= RATE_LIMIT:
        _hits[ip] = recent
        return True
    recent.append(now)
    _hits[ip] = recent
    return False


def validate(data: dict) -> tuple[dict, dict]:
    """Возвращает (чистые данные, ошибки по полям)."""
    clean = {k: str(data.get(k, "")).strip() for k in LIMITS}
    errors = {}
    for field in ("name", "phone", "address", "delivery_time", "items"):
        if not clean[field]:
            errors[field] = "Заполните это поле"
    if "phone" not in errors and not PHONE_RE.match(clean["phone"]):
        errors["phone"] = "Формат: +7 (XXX) XXX-XX-XX"
    for field, limit in LIMITS.items():
        if len(clean[field]) > limit:
            errors[field] = f"Слишком длинный текст (максимум {limit} символов)"
    return clean, errors


def build_message(o: dict) -> str:
    e = lambda s: html.escape(s, quote=False)
    return (
        "📦 <b>НОВЫЙ ЗАКАЗ ИЗ ОНЛАЙН-ФОРМЫ</b>\n"
        f"👤 <b>Имя:</b> {e(o['name'])}\n"
        f"📞 <b>Телефон:</b> {e(o['phone'])}\n"
        f"📍 <b>Адрес:</b> {e(o['address'])}\n"
        f"🕒 <b>Время:</b> {e(o['delivery_time'])}\n\n"
        f"📝 <b>Состав заказа:</b>\n{e(o['items'])}\n\n"
        f"💬 <b>Комментарий:</b> {e(o['comment']) or 'Нет'}"
    )


async def send_to_telegram(session: aiohttp.ClientSession, text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": ADMIN_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    async with session.post(url, json=payload) as resp:
        body = await resp.json(content_type=None)
        if resp.status != 200 or not body.get("ok"):
            raise RuntimeError(f"Telegram error {resp.status}: {body}")


async def handle_order(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        return web.json_response({"ok": False, "error": "Некорректный запрос"}, status=400)

    # Honeypot: боты заполняют скрытое поле — отвечаем "успехом", но ничего не отправляем
    if str(data.get("website", "")).strip():
        return web.json_response({"ok": True})

    if rate_limited(client_ip(request)):
        return web.json_response(
            {"ok": False, "error": "Слишком много заказов. Попробуйте позже."}, status=429
        )

    order, errors = validate(data)
    if errors:
        return web.json_response({"ok": False, "errors": errors}, status=422)

    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN или ADMIN_CHAT_ID не заданы")
        return web.json_response({"ok": False, "error": "Сервис временно недоступен"}, status=500)

    try:
        await send_to_telegram(request.app["session"], build_message(order))
    except Exception:
        log.exception("Не удалось отправить заказ в Telegram")
        return web.json_response(
            {"ok": False, "error": "Не удалось отправить заказ. Попробуйте ещё раз."}, status=502
        )

    log.info("Заказ отправлен: %s", order["phone"])
    return web.json_response({"ok": True})


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def session_ctx(app: web.Application):
    timeout = aiohttp.ClientTimeout(total=10)
    app["session"] = aiohttp.ClientSession(timeout=timeout)
    yield
    await app["session"].close()


def create_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware], client_max_size=64 * 1024)
    app.cleanup_ctx.append(session_ctx)
    app.router.add_post("/api/order", handle_order)
    app.router.add_route("OPTIONS", "/api/order", handle_order)
    app.router.add_get("/health", health)
    app.router.add_get("/", index)
    app.router.add_static("/", STATIC_DIR, show_index=False)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
