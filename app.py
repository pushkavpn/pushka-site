"""Сайт PushkaVPN: личный кабинет, посуточная тарификация устройств, Platega.

Запуск на Railway: gunicorn app:app
"""

import base64
import hashlib
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import jwt
import requests
from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# Конфигурация (переменные окружения сохранены)
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", "/tmp/pushka.db")

SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-railway-env")
SITE_URL = os.environ.get("SITE_URL", "https://your-site.railway.app").rstrip("/")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8867577154:AAHxbdS7xa1-NV2IqQcFf_Ls3oPP5OKXpSA")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "pushkavpn_bot").lstrip("@")

VPS_API_URL = os.environ.get("VPS_API_URL", "http://127.0.0.1:8080").rstrip("/")
VPS_SECRET = os.environ.get("VPS_SECRET", "pushka_secret_2026")
LINK_SECRET = os.environ.get("LINK_SECRET", VPS_SECRET)

# Telegram Login (OIDC) — стандартная авторизация через oauth.telegram.org.
# Client ID и Client Secret выдаёт @BotFather → Login Widget.
TELEGRAM_CLIENT_ID = os.environ.get("TELEGRAM_CLIENT_ID", "").strip()
TELEGRAM_CLIENT_SECRET = os.environ.get("TELEGRAM_CLIENT_SECRET", "").strip()

TG_AUTH_URL = "https://oauth.telegram.org/auth"
TG_TOKEN_URL = "https://oauth.telegram.org/token"
TG_JWKS_URL = "https://oauth.telegram.org/.well-known/jwks.json"
TG_ISSUER = "https://oauth.telegram.org"

# Чат «Служебные уведомления»: уведомление о новой регистрации через Telegram.
# NOTIFY_THREAD_ID — для форум-чатов (идентификатор темы), опционально.
NOTIFY_CHAT_ID = os.environ.get("NOTIFY_CHAT_ID", "").strip()
NOTIFY_THREAD_ID = os.environ.get("NOTIFY_THREAD_ID", "").strip()

# Platega
PLATEGA_URL = "https://app.platega.io/transaction/process"
PLATEGA_MERCHANT_ID = os.environ.get("PLATEGA_MERCHANT_ID", "")
PLATEGA_SECRET = os.environ.get("PLATEGA_SECRET", "")

# ---------------------------------------------------------------------------
# Тарифы (per-device billing): каждому устройству — свой тариф,
# списание раз в сутки суммой стоимости всех активных устройств.
# ---------------------------------------------------------------------------

MIN_TOPUP = 100            # минимальное пополнение себе и другу, ₽
TRIAL_MB = 250             # пробный доступ
TRIAL_HOURS = 1
SUPPORT_URL = "https://t.me/Pushka_Sup"
TG_CHANNEL_URL = os.environ.get("TG_CHANNEL_URL", "").strip()

TARIFFS = {
    "lite": {
        "key": "lite",
        "name": "Лайт",
        "badge": "5 ГБ · 1 устройство",
        "price": 10,
        "gb": 5,
        "devices": 1,
        "price_text": "10₽ в день",
        "desc": "Базовый тариф для одного устройства: 5 ГБ трафика в сутки.",
    },
    "standard": {
        "key": "standard",
        "name": "Стандарт",
        "badge": "15 ГБ · 2 устройства",
        "price": 25,
        "gb": 15,
        "devices": 2,
        "price_text": "25₽ в день",
        "desc": "Оптимум для пары устройств: 15 ГБ трафика в сутки на устройство.",
    },
    "pro": {
        "key": "pro",
        "name": "Про",
        "badge": "40 ГБ · 3 устройства",
        "price": 50,
        "gb": 40,
        "devices": 3,
        "price_text": "50₽ в день",
        "desc": "Для активного использования: 40 ГБ трафика в сутки на устройство.",
    },
    "business": {
        "key": "business",
        "name": "Бизнес",
        "badge": "100 ГБ · 5 устройств",
        "price": 100,
        "gb": 100,
        "devices": 5,
        "price_text": "100₽ в день",
        "desc": "Максимум: 100 ГБ трафика в сутки на устройство, до 5 устройств.",
    },
}

app = Flask(
    __name__,
    static_folder=os.path.join(BASE_DIR, "static"),
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_url_path="/static",
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = SECRET_KEY
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["TEMPLATES_AUTO_RELOAD"] = True
if SITE_URL.startswith("https"):
    app.config["PREFERRED_URL_SCHEME"] = "https"


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE,
    password_hash TEXT,
    telegram_id TEXT UNIQUE,
    telegram_username TEXT,
    telegram_name TEXT,
    telegram_photo TEXT,
    nickname TEXT,
    avatar TEXT,
    balance REAL NOT NULL DEFAULT 0,
    trial_used INTEGER NOT NULL DEFAULT 0,
    trial_until TEXT,
    trial_mb_used REAL NOT NULL DEFAULT 0,
    billing_day TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    link TEXT NOT NULL,
    tariff TEXT,
    suspended INTEGER NOT NULL DEFAULT 0,
    traffic_mb_today REAL NOT NULL DEFAULT 0,
    traffic_day TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS payments (
    tx_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    bonus REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tg_auth (
    token TEXT PRIMARY KEY,
    user_id INTEGER,
    telegram_id TEXT,
    telegram_username TEXT,
    used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promocodes (
    code TEXT PRIMARY KEY,
    bonus REAL NOT NULL,
    max_uses INTEGER NOT NULL DEFAULT 100000
);
CREATE TABLE IF NOT EXISTS promo_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, code)
);
CREATE TABLE IF NOT EXISTS gifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user INTEGER NOT NULL,
    to_user INTEGER NOT NULL,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Миграции со старой схемы (если БД уже существует)
MIGRATIONS = {
    "users": [
        ("telegram_name", "TEXT"),
        ("telegram_photo", "TEXT"),
        ("nickname", "TEXT"),
        ("avatar", "TEXT"),
        ("trial_used", "INTEGER NOT NULL DEFAULT 0"),
        ("trial_until", "TEXT"),
        ("trial_mb_used", "REAL NOT NULL DEFAULT 0"),
        ("billing_day", "TEXT"),
    ],
    "devices": [
        ("tariff", "TEXT"),
        ("suspended", "INTEGER NOT NULL DEFAULT 0"),
        ("traffic_mb_today", "REAL NOT NULL DEFAULT 0"),
        ("traffic_day", "TEXT"),
    ],
    "payments": [
        ("bonus", "REAL NOT NULL DEFAULT 0"),
    ],
}


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        try:
            os.makedirs(db_dir, exist_ok=True)
        except OSError:
            pass
    with sqlite3.connect(DB_PATH) as db:
        cols = [row[1] for row in db.execute("PRAGMA table_info(users)")]
        if cols and "email" not in cols:  # очень старая схема (только telegram_id)
            db.executescript(
                "DROP TABLE IF EXISTS devices;"
                "DROP TABLE IF EXISTS payments;"
                "DROP TABLE IF EXISTS users;"
            )
        db.executescript(SCHEMA)
        db.execute(
            "INSERT OR IGNORE INTO promocodes (code, bonus, max_uses) VALUES (?, ?, ?)",
            ("PUSHKA2026", 100, 100000),
        )
        for table, columns in MIGRATIONS.items():
            existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            for col, decl in columns:
                if col not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def today() -> str:
    return now().date().isoformat()


def get_user(user_id):
    if user_id is None:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_devices(user_id) -> list:
    return get_db().execute(
        "SELECT * FROM devices WHERE user_id = ? ORDER BY id DESC", (user_id,)
    ).fetchall()


def display_name(user) -> str:
    if user is None:
        return "Пользователь"
    return (
        (user["nickname"] or "").strip()
        or (user["telegram_name"] or "").strip()
        or (user["telegram_username"] or "").strip()
        or ((user["email"] or "").split("@")[0] if user["email"] else "")
        or "Пользователь"
    )


def avatar_src(user) -> str:
    if user is None:
        return ""
    return (user["avatar"] or user["telegram_photo"] or "").strip()


def initials(user) -> str:
    name = display_name(user)
    parts = [p for p in name.replace("@", " ").split() if p]
    if not parts:
        return "PV"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[1][:1]).upper()


# ---------------------------------------------------------------------------
# Тарификация: посуточное списание и пробный доступ
# ---------------------------------------------------------------------------


def trial_active(user) -> bool:
    """Пробный доступ: 250 МБ на 1 час, один раз на аккаунт."""
    if not user or not user["trial_used"] or not user["trial_until"]:
        return False
    if user["trial_mb_used"] >= TRIAL_MB:
        return False
    try:
        until = datetime.fromisoformat(user["trial_until"])
    except (TypeError, ValueError):
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > now()


def device_usable(user, device) -> bool:
    """Устройство работает, если: не отключено и (есть тариф или идёт триал)."""
    if device["suspended"]:
        return False
    return bool(device["tariff"]) or trial_active(user)


def daily_total(devices) -> float:
    """Стоимость всех активных устройств за сутки."""
    return sum(
        TARIFFS[d["tariff"]]["price"]
        for d in devices
        if d["tariff"] and d["tariff"] in TARIFFS and not d["suspended"]
    )


def ensure_billing(user):
    """Сброс дневного трафика + суточное списание стоимости устройств.

    Раз в сутки суммируем стоимость всех активных устройств и списываем её.
    Если баланса не хватает — все устройства отключаются до пополнения.
    """
    if user is None:
        return None
    db = get_db()
    devices = get_devices(user["id"])

    # Новый день — обнуляем дневной трафик устройств
    for d in devices:
        if d["traffic_day"] != today():
            db.execute(
                "UPDATE devices SET traffic_mb_today = 0, traffic_day = ? WHERE id = ?",
                (today(), d["id"]),
            )
    if user["billing_day"] == today():
        db.commit()
        return get_user(user["id"])

    # Суточное списание
    total = daily_total(devices)
    if total > 0:
        if user["balance"] >= total:
            db.execute(
                "UPDATE users SET balance = balance - ?, billing_day = ? WHERE id = ?",
                (total, today(), user["id"]),
            )
        else:
            # Баланса не хватает — отключаем все устройства до пополнения
            db.execute(
                "UPDATE devices SET suspended = 1 WHERE user_id = ?",
                (user["id"],),
            )
            db.execute(
                "UPDATE users SET billing_day = ? WHERE id = ?", (today(), user["id"])
            )
    else:
        db.execute(
            "UPDATE users SET billing_day = ? WHERE id = ?", (today(), user["id"])
        )
    db.commit()
    return get_user(user["id"])


def resume_devices(user_id: int) -> None:
    """Включает устройства обратно после успешного пополнения баланса."""
    db = get_db()
    db.execute("UPDATE devices SET suspended = 0 WHERE user_id = ?", (user_id,))
    db.commit()


def activate_trial(user_id):
    """Пробный доступ: 250 МБ на 1 час, один раз на аккаунт."""
    user = get_user(user_id)
    if user["trial_used"]:
        return False, "Пробный доступ уже использован на этом аккаунте"
    db = get_db()
    db.execute(
        "UPDATE users SET trial_used = 1, trial_mb_used = 0, trial_until = ? WHERE id = ?",
        ((now() + timedelta(hours=TRIAL_HOURS)).isoformat(timespec="seconds"), user_id),
    )
    db.commit()
    return True, f"Активировано: {TRIAL_MB} МБ на {TRIAL_HOURS} час"


def apply_usage(user_id, mb: float, device_id=None):
    """VPS сообщает о потраченном трафике. Возвращает (active, message)."""
    db = get_db()
    user = ensure_billing(get_user(user_id))
    if user is None:
        return False, "Пользователь не найден"
    mb = max(0.0, float(mb))

    device = None
    if device_id is not None:
        device = db.execute(
            "SELECT * FROM devices WHERE id = ? AND user_id = ?",
            (device_id, user["id"]),
        ).fetchone()
        if device is None:
            return False, "Устройство не найдено"

    if device is None:
        # Старый формат API (без device_id): берём первое устройство с тарифом,
        # иначе считаем пробный трафик.
        devices = get_devices(user["id"])
        for d in devices:
            if d["tariff"] and not d["suspended"]:
                device = d
                break

    if device is not None and device["tariff"] and not device["suspended"]:
        # Устройство с тарифом — учитываем дневной лимит тарифа
        limit_mb = TARIFFS[device["tariff"]]["gb"] * 1024
        used = device["traffic_mb_today"] + mb
        if used >= limit_mb:
            db.execute(
                "UPDATE devices SET traffic_mb_today = ?, traffic_day = ? WHERE id = ?",
                (limit_mb, today(), device["id"]),
            )
            db.commit()
            return False, (
                f"Лимит {TARIFFS[device['tariff']]['gb']} ГБ на сегодня "
                f"по устройству «{device['name']}» исчерпан"
            )
        db.execute(
            "UPDATE devices SET traffic_mb_today = ?, traffic_day = ? WHERE id = ?",
            (used, today(), device["id"]),
        )
        db.commit()
        left = int(limit_mb - used)
        return True, f"Осталось {left} МБ из {limit_mb} МБ (устройство «{device['name']}»)"

    # Нет активного тарифа — засчитываем в триал
    if trial_active(user):
        used = user["trial_mb_used"] + mb
        if used >= TRIAL_MB:
            db.execute(
                "UPDATE users SET trial_mb_used = ?, trial_until = NULL WHERE id = ?",
                (TRIAL_MB, user["id"]),
            )
            db.commit()
            return False, "Пробный лимит 250 МБ исчерпан — выберите тариф"
        db.execute(
            "UPDATE users SET trial_mb_used = ? WHERE id = ?", (used, user["id"])
        )
        db.commit()
        return True, f"Пробный доступ: осталось {int(TRIAL_MB - used)} МБ"

    if device is not None and device["suspended"]:
        return False, "Устройство отключено — пополните баланс"
    return False, "Нет активного тарифа — назначьте тариф устройству"


# ---------------------------------------------------------------------------
# Авторизация: email + пароль и вход через Telegram
# ---------------------------------------------------------------------------


def current_user():
    return get_user(session.get("user_id"))


def login_user(user) -> None:
    session.clear()
    session["user_id"] = user["id"]


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-zА-Яа-я]{2,}$")


def _mtime(*rel):
    path = os.path.join(BASE_DIR, *rel)
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


@app.context_processor
def inject_globals():
    static_v = str(
        max(
            _mtime("static", "css", "style.css"),
            _mtime("static", "script.js"),
            _mtime("static", "img", "pumpkin.png"),
            _mtime("static", "img", "logo.png"),
        )
    )
    return {
        "current_user": current_user(),
        "tariffs": TARIFFS,
        "min_topup": MIN_TOPUP,
        "trial_mb": TRIAL_MB,
        "bot_username": BOT_USERNAME,
        "support_url": SUPPORT_URL,
        "tg_channel_url": TG_CHANNEL_URL,
        "static_v": static_v,
        "display_name": display_name,
        "avatar_src": avatar_src,
        "initials": initials,
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    error = request.args.get("error") if request.method == "GET" else None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = get_db().execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
        if not user or not user["password_hash"] or not check_password_hash(
            user["password_hash"], password
        ):
            error = "Неверная почта или пароль."
        else:
            login_user(user)
            return redirect(url_for("dashboard"))
    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""

        if not EMAIL_RE.match(email):
            error = "Введите корректный email."
        elif len(password) < 6:
            error = "Пароль должен быть не короче 6 символов."
        elif password != password2:
            error = "Пароли не совпадают."
        else:
            db = get_db()
            if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                error = "Аккаунт с такой почтой уже существует."
            else:
                cur = db.execute(
                    "INSERT INTO users (email, password_hash, created_at) "
                    "VALUES (?, ?, ?)",
                    (email, generate_password_hash(password), now_iso()),
                )
                db.commit()
                user = get_user(cur.lastrowid)
                login_user(user)
                return redirect(url_for("dashboard"))
    return render_template("register.html", error=error)


# ---------------------------------------------------------------------------
# Telegram Login (OIDC): редирект на oauth.telegram.org, аккаунт создаётся сам
# ---------------------------------------------------------------------------


def _tg_redirect_uri() -> str:
    return f"{SITE_URL}/auth/telegram/callback"


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _notify(text: str) -> None:
    """Уведомление в чат «Служебные уведомления» через Bot API."""
    if not NOTIFY_CHAT_ID or not BOT_TOKEN:
        return
    payload = {"chat_id": NOTIFY_CHAT_ID, "text": text}
    if NOTIFY_THREAD_ID.isdigit():
        payload["message_thread_id"] = int(NOTIFY_THREAD_ID)
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("notify error: %s", exc)


def _tg_exchange_and_verify(code: str, verifier: str) -> dict:
    """Меняет код на id_token и проверяет подпись JWT (JWKS от oauth.telegram.org)."""
    resp = requests.post(
        TG_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _tg_redirect_uri(),
            "client_id": TELEGRAM_CLIENT_ID,
            "code_verifier": verifier,
        },
        auth=(TELEGRAM_CLIENT_ID, TELEGRAM_CLIENT_SECRET),
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"token endpoint HTTP {resp.status_code}")
    id_token = (resp.json() or {}).get("id_token")
    if not id_token:
        raise RuntimeError("id_token отсутствует в ответе")

    jwks = jwt.PyJWKClient(TG_JWKS_URL, cache_keys=True)
    signing_key = jwks.get_signing_key_from_jwt(id_token)
    return jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256", "ES256"],
        audience=str(TELEGRAM_CLIENT_ID),
        issuer=TG_ISSUER,
        options={"require": ["exp", "iat", "sub"]},
    )


def _tg_login_or_register(claims: dict):
    """Вход по telegram_id; при отсутствии аккаунта — регистрация.

    Имя, username и фото профиля берутся из Telegram.
    """
    db = get_db()
    tg_id = str(claims.get("sub") or "")
    username = str(claims.get("preferred_username") or "")
    name = str(claims.get("name") or "").strip()
    photo = str(claims.get("picture") or "")
    if not tg_id:
        raise RuntimeError("в id_token нет sub")

    user = db.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (tg_id,)
    ).fetchone()
    created = False

    if user is None:
        session_user = get_user(session.get("user_id"))
        if session_user is not None and not session_user["telegram_id"]:
            # Уже открыт аккаунт по почте — привязываем Telegram к нему
            db.execute(
                "UPDATE users SET telegram_id = ?, telegram_username = ?, "
                "telegram_name = ?, telegram_photo = ? WHERE id = ?",
                (tg_id, username, name, photo, session_user["id"]),
            )
            user = get_user(session_user["id"])
        else:
            cur = db.execute(
                "INSERT INTO users (telegram_id, telegram_username, telegram_name, "
                "telegram_photo, created_at) VALUES (?, ?, ?, ?, ?)",
                (tg_id, username, name, photo, now_iso()),
            )
            user = get_user(cur.lastrowid)
            created = True
    else:
        db.execute(
            "UPDATE users SET telegram_username = ?, telegram_name = ?, "
            "telegram_photo = ? WHERE id = ?",
            (username, name, photo, user["id"]),
        )
        user = get_user(user["id"])

    db.commit()
    return user, created


@app.get("/auth/telegram")
def tg_oauth():
    """Кнопка «Войти через Telegram»: редирект на oauth.telegram.org."""
    if not TELEGRAM_CLIENT_ID or not TELEGRAM_CLIENT_SECRET:
        return redirect(url_for("login", error="tg_unavailable"))
    state = secrets.token_urlsafe(18)
    verifier = secrets.token_urlsafe(48)
    session["tg_state"] = state
    session["tg_verifier"] = verifier
    params = urlencode(
        {
            "client_id": TELEGRAM_CLIENT_ID,
            "redirect_uri": _tg_redirect_uri(),
            "response_type": "code",
            "scope": "openid profile",
            "state": state,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
            "lang": "ru",
        }
    )
    return redirect(f"{TG_AUTH_URL}?{params}")


@app.get("/auth/telegram/callback")
def tg_oauth_callback():
    """Приём кода от oauth.telegram.org: вход или автоматическая регистрация."""
    if request.args.get("error") or not request.args.get("code"):
        return redirect(url_for("login", error="telegram"))
    if not request.args.get("state") or request.args.get("state") != session.pop(
        "tg_state", None
    ):
        return redirect(url_for("login", error="telegram"))
    verifier = session.pop("tg_verifier", None)
    if not verifier:
        return redirect(url_for("login", error="telegram"))

    try:
        claims = _tg_exchange_and_verify(request.args["code"], verifier)
        user, created = _tg_login_or_register(claims)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("telegram oauth error: %s", exc)
        return redirect(url_for("login", error="telegram"))

    login_user(user)
    if created:
        _notify(
            "🔔 Новая регистрация через Telegram\n"
            f"Имя: {claims.get('name') or '—'}\n"
            f"Username: @{claims.get('preferred_username') or '—'}\n"
            f"Telegram ID: {claims.get('sub')}\n"
            f"ID на сайте: #{user['id']}"
        )
    return redirect(url_for("dashboard"))


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@app.post("/auth/telegram/start")
def tg_start():
    """Создаёт одноразовую ссылку t.me/<bot>?start=auth_<token>."""
    token = secrets.token_urlsafe(18)
    db = get_db()
    db.execute(
        "INSERT INTO tg_auth (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (
            token,
            session.get("user_id"),
            now_iso(),
            (now() + timedelta(minutes=15)).isoformat(timespec="seconds"),
        ),
    )
    db.commit()
    return jsonify(
        success=True,
        token=token,
        url=f"https://t.me/{BOT_USERNAME}?start=auth_{token}",
    )


@app.post("/auth/telegram/confirm")
def tg_confirm():
    """Бот подтверждает вход: присылает telegram_id для токена."""
    if request.headers.get("X-Secret") != LINK_SECRET:
        return jsonify(success=False, error="Unauthorized"), 401

    data = request.get_json(silent=True) or {}
    token = str(data.get("token") or "")
    telegram_id = str(data.get("telegram_id") or "")
    username = str(data.get("username") or "")
    if not token or not telegram_id.isdigit():
        return jsonify(success=False, error="bad params"), 400

    db = get_db()
    row = db.execute("SELECT * FROM tg_auth WHERE token = ?", (token,)).fetchone()
    if row is None or row["used"]:
        return jsonify(success=False, error="token not found"), 404
    expires = _parse_iso(row["expires_at"])
    if expires is None or expires < now():
        return jsonify(success=False, error="token expired"), 410

    user = db.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()

    if user is None and row["user_id"]:
        user = get_user(row["user_id"])
        if user:
            db.execute(
                "UPDATE users SET telegram_id = ?, telegram_username = ? WHERE id = ?",
                (telegram_id, username, user["id"]),
            )

    if user is None:
        cur = db.execute(
            "INSERT INTO users (telegram_id, telegram_username, created_at) "
            "VALUES (?, ?, ?)",
            (telegram_id, username, now_iso()),
        )
        user = get_user(cur.lastrowid)
    elif username and user["telegram_username"] != username:
        db.execute(
            "UPDATE users SET telegram_username = ? WHERE id = ?", (username, user["id"])
        )

    db.execute(
        "UPDATE tg_auth SET used = 1, user_id = ?, telegram_id = ?, telegram_username = ? "
        "WHERE token = ?",
        (user["id"], telegram_id, username, token),
    )
    db.commit()
    return jsonify(success=True, user_id=user["id"])


@app.get("/auth/telegram/status")
def tg_status():
    """Браузер опрашивает статус и получает готовую сессию."""
    token = request.args.get("token") or ""
    db = get_db()
    row = db.execute("SELECT * FROM tg_auth WHERE token = ?", (token,)).fetchone()
    if row is None or not row["used"]:
        return jsonify(confirmed=False)
    user = get_user(row["user_id"])
    if user:
        login_user(user)
    return jsonify(confirmed=True, logged_in=bool(user))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Главная и информационные страницы
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/rules")
def rules():
    return render_template("page.html", title="Правила сервиса", body=RULES_TEXT)


@app.route("/privacy")
def privacy():
    return render_template("page.html", title="Политика конфиденциальности", body=PRIVACY_TEXT)


@app.route("/terms")
def terms():
    return render_template("page.html", title="Пользовательское соглашение", body=TERMS_TEXT)


@app.route("/support")
def support():
    return redirect(SUPPORT_URL)


# ---------------------------------------------------------------------------
# Личный кабинет
# ---------------------------------------------------------------------------


def _cabinet_ctx(tab: str):
    user = current_user()
    if not user:
        return None
    user = ensure_billing(user)
    devices = get_devices(user["id"])
    payments = get_db().execute(
        "SELECT * FROM payments WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
        (user["id"],),
    ).fetchall()
    gifts = get_db().execute(
        "SELECT * FROM gifts WHERE from_user = ? OR to_user = ? ORDER BY id DESC LIMIT 30",
        (user["id"], user["id"]),
    ).fetchall()
    state = {
        "user": user,
        "devices": devices,
        "daily_total": daily_total(devices),
        "active_devices": sum(1 for d in devices if device_usable(user, d)),
        "trial_active": trial_active(user),
        "suspended": any(d["suspended"] for d in devices),
        "tab": tab,
        "name": display_name(user),
        "avatar": avatar_src(user),
        "initials": initials(user),
        "payments": payments,
        "gifts": gifts,
    }
    return user, devices, state


@app.route("/dashboard")
def dashboard():
    ctx = _cabinet_ctx("home")
    if not ctx:
        return redirect(url_for("login"))
    user, devices, state = ctx
    return render_template("dashboard.html", user=user, state=state, devices=devices)


@app.route("/dashboard/devices")
def dashboard_devices():
    ctx = _cabinet_ctx("devices")
    if not ctx:
        return redirect(url_for("login"))
    user, devices, state = ctx
    return render_template("dashboard.html", user=user, state=state, devices=devices)


@app.route("/dashboard/balance")
def dashboard_balance():
    ctx = _cabinet_ctx("balance")
    if not ctx:
        return redirect(url_for("login"))
    user, devices, state = ctx
    return render_template("dashboard.html", user=user, state=state, devices=devices)


@app.route("/dashboard/profile")
def dashboard_profile():
    ctx = _cabinet_ctx("profile")
    if not ctx:
        return redirect(url_for("login"))
    user, devices, state = ctx
    return render_template("dashboard.html", user=user, state=state, devices=devices)


@app.route("/topup")
def topup():
    """Страница пополнения баланса (минимум 100 ₽)."""
    return redirect(url_for("dashboard_balance"))


@app.post("/profile")
def update_profile():
    user = current_user()
    if not user:
        return jsonify(success=False, error="Требуется вход"), 401
    data = request.get_json(silent=True) or request.form or {}
    nickname = str(data.get("nickname") or "").strip()
    if len(nickname) > 40:
        return jsonify(success=False, error="Ник не длиннее 40 символов"), 400
    avatar = str(data.get("avatar") or "").strip()
    if avatar and not (avatar.startswith("http://") or avatar.startswith("https://") or avatar.startswith("data:image/")):
        return jsonify(success=False, error="Аватар — URL или data:image"), 400
    if avatar and len(avatar) > 900000:
        return jsonify(success=False, error="Слишком большой аватар"), 400
    db = get_db()
    if nickname:
        db.execute("UPDATE users SET nickname = ? WHERE id = ?", (nickname, user["id"]))
    if "avatar" in data:
        db.execute("UPDATE users SET avatar = ? WHERE id = ?", (avatar or None, user["id"]))
    db.commit()
    user = get_user(user["id"])
    return jsonify(success=True, nickname=display_name(user), avatar=avatar_src(user))


@app.post("/profile/password")
def change_password():
    user = current_user()
    if not user:
        return jsonify(success=False, error="Требуется вход"), 401
    data = request.get_json(silent=True) or {}
    current = str(data.get("current") or "")
    new_password = str(data.get("password") or "")
    password2 = str(data.get("password2") or new_password)
    if len(new_password) < 6:
        return jsonify(success=False, error="Пароль должен быть не короче 6 символов"), 400
    if new_password != password2:
        return jsonify(success=False, error="Пароли не совпадают"), 400
    if user["password_hash"]:
        if not current or not check_password_hash(user["password_hash"], current):
            return jsonify(success=False, error="Неверный текущий пароль"), 400
    db = get_db()
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), user["id"]),
    )
    db.commit()
    return jsonify(success=True)


@app.post("/promo")
def apply_promo():
    user = current_user()
    if not user:
        return jsonify(success=False, error="Требуется вход"), 401
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "").strip().upper()
    if not code:
        return jsonify(success=False, error="Введите промокод"), 400
    db = get_db()
    promo = db.execute("SELECT * FROM promocodes WHERE code = ?", (code,)).fetchone()
    if promo is None:
        return jsonify(success=False, error="Промокод не найден"), 404
    used = db.execute(
        "SELECT 1 FROM promo_uses WHERE user_id = ? AND code = ?", (user["id"], code)
    ).fetchone()
    if used:
        return jsonify(success=False, error="Промокод уже использован"), 409
    total = db.execute(
        "SELECT COUNT(*) FROM promo_uses WHERE code = ?", (code,)
    ).fetchone()[0]
    if total >= promo["max_uses"]:
        return jsonify(success=False, error="Промокод исчерпан"), 410
    db.execute(
        "INSERT INTO promo_uses (user_id, code, created_at) VALUES (?, ?, ?)",
        (user["id"], code, now_iso()),
    )
    db.execute(
        "UPDATE users SET balance = balance + ? WHERE id = ?",
        (promo["bonus"], user["id"]),
    )
    db.commit()
    return jsonify(success=True, bonus=promo["bonus"], balance=get_user(user["id"])["balance"])


@app.post("/gift")
def gift_balance():
    user = current_user()
    if not user:
        return jsonify(success=False, error="Требуется вход"), 401
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    target = str(data.get("to") or data.get("email") or "").strip()
    if amount < MIN_TOPUP:
        return jsonify(success=False, error=f"Минимальная сумма — {MIN_TOPUP} ₽"), 400
    if user["balance"] < amount:
        return jsonify(success=False, error="Недостаточно средств"), 400
    db = get_db()
    friend = None
    if target.isdigit():
        friend = get_user(int(target))
    if friend is None and target:
        friend = db.execute(
            "SELECT * FROM users WHERE email = ? OR telegram_username = ? OR nickname = ?",
            (target.lower(), target.lstrip("@"), target),
        ).fetchone()
    if friend is None:
        return jsonify(success=False, error="Пользователь не найден"), 404
    if friend["id"] == user["id"]:
        return jsonify(success=False, error="Нельзя пополнить самому себе"), 400
    db.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (amount, user["id"]))
    db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, friend["id"]))
    db.execute(
        "INSERT INTO gifts (from_user, to_user, amount, created_at) VALUES (?, ?, ?, ?)",
        (user["id"], friend["id"], amount, now_iso()),
    )
    db.commit()
    return jsonify(success=True, balance=get_user(user["id"])["balance"])


@app.post("/trial")
def trial():
    """Активация пробного доступа: 250 МБ / 1 час, один раз на аккаунт."""
    user = current_user()
    if not user:
        return jsonify(success=False, error="Требуется вход"), 401
    ok, msg = activate_trial(user["id"])
    return jsonify(success=ok, error=None if ok else msg, message=msg), 200 if ok else 402


def _tariff_cap_error(user_id, tariff_key, exclude_device_id=None) -> str | None:
    """Проверяет лимит устройств тарифа. Возвращает текст ошибки или None."""
    cap = TARIFFS[tariff_key]["devices"]
    db = get_db()
    query = "SELECT COUNT(*) FROM devices WHERE user_id = ? AND tariff = ?"
    params = [user_id, tariff_key]
    if exclude_device_id is not None:
        query += " AND id != ?"
        params.append(exclude_device_id)
    count = db.execute(query, params).fetchone()[0]
    if count >= cap:
        return f"Тариф «{TARIFFS[tariff_key]['name']}» — максимум {cap} устройств"
    return None


@app.post("/device")
def add_device():
    """Подключает устройство (через API VPS) и по желанию назначает тариф."""
    user = current_user()
    if not user:
        return jsonify(success=False, error="Требуется вход"), 401
    user = ensure_billing(user)

    data = request.get_json(silent=True) or {}
    tariff = str(data.get("tariff") or "").strip() or None
    if tariff is not None and tariff not in TARIFFS:
        return jsonify(success=False, error="Неизвестный тариф"), 400
    if tariff is not None:
        cap_error = _tariff_cap_error(user["id"], tariff)
        if cap_error:
            return jsonify(success=False, error=cap_error), 409

    link = _create_vless_link(user["id"])
    if not link:
        return jsonify(success=False, error="Не удалось создать устройство"), 502
    db = get_db()
    count = db.execute(
        "SELECT COUNT(*) FROM devices WHERE user_id = ?", (user["id"],)
    ).fetchone()[0]
    db.execute(
        "INSERT INTO devices (user_id, name, link, tariff, traffic_day, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"], f"Устройство {count + 1}", link, tariff, today(), now_iso()),
    )
    db.commit()
    return jsonify(success=True, link=link, tariff=tariff)


def _create_vless_link(user_id: int) -> str | None:
    """Создаёт vless:// через VPS API; если API недоступен — локальный UUID-линк."""
    try:
        resp = requests.post(
            f"{VPS_API_URL}/create",
            headers={"X-Secret": VPS_SECRET},
            json={"user_id": user_id},
            timeout=12,
        )
        payload = resp.json() if getattr(resp, "ok", resp.status_code == 200) else {}
        link = payload.get("link") or payload.get("vless")
        if payload.get("success") and link and str(link).startswith("vless://"):
            return str(link)
        if link and str(link).startswith("vless://"):
            return str(link)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("VPS create error: %s", exc)

    uuid = secrets.token_hex(16)
    uuid = f"{uuid[:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:]}"
    host = os.environ.get("VPN_HOST", "vpn.pushkavpn.net")
    port = os.environ.get("VPN_PORT", "443")
    sni = os.environ.get("VPN_SNI", host)
    return (
        f"vless://{uuid}@{host}:{port}"
        f"?encryption=none&flow=xtls-rprx-vision&security=reality"
        f"&sni={sni}&fp=chrome&type=tcp#PushkaVPN-{user_id}"
    )


@app.post("/device/<int:device_id>/tariff")
def set_device_tariff(device_id: int):
    """Назначает (или снимает) тариф у устройства."""
    user = current_user()
    if not user:
        return jsonify(success=False, error="Требуется вход"), 401
    user = ensure_billing(user)

    data = request.get_json(silent=True) or {}
    tariff = str(data.get("tariff") or "").strip()
    db = get_db()
    device = db.execute(
        "SELECT * FROM devices WHERE id = ? AND user_id = ?",
        (device_id, user["id"]),
    ).fetchone()
    if device is None:
        return jsonify(success=False, error="Устройство не найдено"), 404

    if tariff == "":  # снять тариф
        db.execute("UPDATE devices SET tariff = NULL WHERE id = ?", (device_id,))
        db.commit()
        return jsonify(success=True, tariff=None)

    if tariff not in TARIFFS:
        return jsonify(success=False, error="Неизвестный тариф"), 400
    cap_error = _tariff_cap_error(user["id"], tariff, exclude_device_id=device_id)
    if cap_error:
        return jsonify(success=False, error=cap_error), 409

    db.execute("UPDATE devices SET tariff = ? WHERE id = ?", (tariff, device_id))
    db.commit()
    return jsonify(success=True, tariff=tariff)


@app.delete("/device/<int:device_id>")
def delete_device(device_id: int):
    user = current_user()
    if not user:
        return jsonify(success=False, error="Требуется вход"), 401
    db = get_db()
    db.execute(
        "DELETE FROM devices WHERE id = ? AND user_id = ?", (device_id, user["id"])
    )
    db.commit()
    return jsonify(success=True)


@app.post("/usage")
def usage():
    """VPS сообщает о потраченном трафике: {"user_id": 1, "mb": 120, "device_id": 2}."""
    if request.headers.get("X-Secret") != VPS_SECRET:
        return jsonify(success=False, error="Unauthorized"), 401
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    if not user_id:
        return jsonify(success=False, error="user_id required"), 400
    active, message = apply_usage(
        user_id, data.get("mb", 0), device_id=data.get("device_id")
    )
    return jsonify(success=True, active=active, message=message)


# ---------------------------------------------------------------------------
# Platega: создание платежа и колбэк
# ---------------------------------------------------------------------------


@app.post("/pay")
def pay():
    """Создаёт платёж. Пополнение от 100 ₽."""
    user = current_user()
    if not user:
        return jsonify(success=False, error="Требуется вход"), 401

    try:
        amount = float((request.get_json(silent=True) or {}).get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount < MIN_TOPUP:
        return (
            jsonify(success=False, error=f"Минимальная сумма пополнения — {MIN_TOPUP} ₽"),
            400,
        )

    payload = {
        "paymentMethod": 2,
        "paymentDetails": {"amount": amount, "currency": "RUB"},
        "description": f"Пополнение баланса #{user['id']}",
        "return": f"{SITE_URL}/dashboard",
    }
    headers = {
        "X-MerchantId": PLATEGA_MERCHANT_ID,
        "X-Secret": PLATEGA_SECRET,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            PLATEGA_URL, json=payload, headers=headers, allow_redirects=False, timeout=30
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify(success=False, error=f"Platega недоступна: {exc}"), 502

    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("Location")
        if location:
            return jsonify(success=True, url=location)

    try:
        data = resp.json()
    except ValueError:
        data = {}

    url = (
        data.get("redirect")
        or data.get("url")
        or data.get("paymentUrl")
        or data.get("link")
        or data.get("payment_link")
    )
    if url:
        return jsonify(success=True, url=url)

    return (
        jsonify(success=False, error=data.get("message") or f"Ошибка Platega ({resp.status_code})"),
        502,
    )


@app.post("/platega/callback")
def platega_callback():
    data = request.get_json(silent=True) or {}

    status = str(data.get("status") or data.get("transactionStatus") or "").upper()
    if status != "CONFIRMED":
        return jsonify(success=True, skipped=True)

    amount = _to_float(
        data.get("amount")
        or (data.get("paymentDetails") or {}).get("amount")
        or data.get("total")
    )
    if amount is None:
        return jsonify(success=False, error="amount not found"), 400

    user_id = _extract_user_id(data)
    if not user_id or get_user(user_id) is None:
        return jsonify(success=False, error="user not found"), 400

    tx_id = str(
        data.get("transactionId")
        or data.get("id")
        or data.get("invoiceId")
        or f"{user_id}-{amount}-{data.get('createdAt', now_iso())}"
    )

    db = get_db()
    if db.execute("SELECT 1 FROM payments WHERE tx_id = ?", (tx_id,)).fetchone():
        return jsonify(success=True, duplicate=True)

    bonus = 0.0
    db.execute(
        "UPDATE users SET balance = balance + ? WHERE id = ?", (amount + bonus, user_id)
    )
    db.execute(
        "INSERT INTO payments (tx_id, user_id, amount, bonus, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (tx_id, user_id, amount, bonus, now_iso()),
    )
    db.commit()

    # Пополнение включает устройства, отключённые из-за нехватки баланса
    resume_devices(user_id)
    return jsonify(success=True, bonus=bonus)


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_user_id(data: dict):
    for key in ("user_id", "userId", "metadata"):
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("user_id") or value.get("userId")
        if value is not None and str(value).isdigit():
            return int(value)

    # id пользователя приезжает в описании: "Пополнение баланса #1"
    match = re.search(r"#(\d+)", str(data.get("description") or ""))
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Тексты страниц
# ---------------------------------------------------------------------------

RULES_TEXT = f"""
<h2>1. Общие положения</h2>
<p>PushkaVPN предоставляет доступ к защищённому соединению на условиях настоящих Правил.
Используя сервис, вы подтверждаете согласие с ними.</p>

<h2>2. Тарифы и списания</h2>
<p>Каждому устройству назначается свой тариф. Тарифы списываются раз в сутки:
система суммирует стоимость всех активных устройств и списывает общую сумму с баланса.</p>
<p><b>Лайт</b> — {TARIFFS['lite']['price']} ₽/день, {TARIFFS['lite']['gb']} ГБ/день, до {TARIFFS['lite']['devices']} устройства.</p>
<p><b>Стандарт</b> — {TARIFFS['standard']['price']} ₽/день, {TARIFFS['standard']['gb']} ГБ/день, до {TARIFFS['standard']['devices']} устройств.</p>
<p><b>Про</b> — {TARIFFS['pro']['price']} ₽/день, {TARIFFS['pro']['gb']} ГБ/день, до {TARIFFS['pro']['devices']} устройств.</p>
<p><b>Бизнес</b> — {TARIFFS['business']['price']} ₽/день, {TARIFFS['business']['gb']} ГБ/день, до {TARIFFS['business']['devices']} устройств.</p>
<p>Если баланса не хватает — все устройства отключаются до пополнения.
Пополнение от {MIN_TOPUP} ₽.</p>

<h2>3. Пробный доступ</h2>
<p>{TRIAL_MB} МБ трафика на {TRIAL_HOURS} час, один раз на аккаунт — без оплаты.</p>

<h2>4. Устройства</h2>
<p>Количество подключаемых устройств не ограничено, но лимиты по числу устройств
действуют для каждого тарифа. Ссылку vless:// нельзя передавать третьим лицам —
доступ привязан к вашему аккаунту.</p>

<h2>5. Ответственность</h2>
<p>Сервис не отвечает за работу сторонних ресурсов и за ограничения, применяемые
на территории использования. Запрещено использование сервиса для противоправной деятельности.</p>
"""

PRIVACY_TEXT = """
<p class="doc-date">1 октября 2026</p>

<p>Политика конфиденциальности регулирует сбор, использование и защиту информации
пользователей сервиса PushkaVPN. Собираются идентификаторы аккаунта, техническая
информация и история взаимодействий. Данные используются для обеспечения работы
сервиса, связи с пользователем и анализа. Передача информации третьим лицам возможна
только в законодательно установленных случаях или с согласия пользователя. Хранение
данных осуществляется в течение необходимого срока, их защита — в разумных пределах.
Пользователь самостоятельно несёт ответственность за риски, связанные с передачей
данных. Администрация вправе вносить изменения в Политику без уведомления — согласие
считается принятым при дальнейшем использовании сервиса.</p>

<h2>1. Общие положения</h2>
<p>1.1. Настоящая Политика конфиденциальности (далее — «Политика») регулирует порядок
обработки и защиты информации, которую Пользователь передаёт при использовании сервиса
PushkaVPN (далее — «Сервис»).</p>
<p>1.2. Используя Сервис, Пользователь подтверждает своё согласие с условиями Политики.
Если Пользователь не согласен с условиями — он обязан прекратить использование Сервиса.</p>

<h2>2. Сбор информации</h2>
<p>2.1. Сервис может собирать следующие типы данных:</p>
<ul>
<li>идентификаторы аккаунта (логин, ID, никнейм и т.п.);</li>
<li>техническую информацию (IP-адрес, данные о браузере, устройстве и операционной системе);</li>
<li>историю взаимодействий с Сервисом.</li>
</ul>
<p>2.2. Сервис не требует от Пользователя предоставления паспортных данных, документов,
фотографий или другой личной информации, кроме минимально необходимой для работы.</p>

<h2>3. Использование информации</h2>
<p>3.1. Сервис может использовать полученную информацию исключительно для:</p>
<ul>
<li>обеспечения работы функционала;</li>
<li>связи с Пользователем (в том числе для уведомлений и поддержки);</li>
<li>анализа и улучшения работы Сервиса.</li>
</ul>

<h2>4. Передача информации третьим лицам</h2>
<p>4.1. Администрация не передаёт полученные данные третьим лицам, за исключением случаев:</p>
<ul>
<li>если это требуется по закону;</li>
<li>если это необходимо для исполнения обязательств перед Пользователем
(например, при работе с платёжными системами);</li>
<li>если Пользователь сам дал на это согласие.</li>
</ul>

<h2>5. Хранение и защита данных</h2>
<p>5.1. Данные хранятся в течение срока, необходимого для достижения целей обработки.</p>
<p>5.2. Администрация принимает разумные меры для защиты данных, но не гарантирует
абсолютную безопасность информации при передаче через интернет.</p>

<h2>6. Отказ от ответственности</h2>
<p>6.1. Пользователь понимает и соглашается, что передача информации через интернет
всегда сопряжена с рисками.</p>
<p>6.2. Администрация не несёт ответственности за утрату, кражу или раскрытие данных,
если это произошло по вине третьих лиц или самого Пользователя.</p>

<h2>7. Изменения в Политике</h2>
<p>7.1. Администрация вправе изменять условия Политики без предварительного уведомления.</p>
<p>7.2. Продолжение использования Сервиса после внесения изменений означает согласие
Пользователя с новой редакцией Политики.</p>
"""

TERMS_TEXT = """
<p class="doc-date">1 октября 2026</p>

<h2>1. Общие положения</h2>
<p>1.1. Настоящее Пользовательское соглашение (далее — «Соглашение») регулирует порядок
использования онлайн-сервиса PushkaVPN (далее — «Сервис»), предоставляемого Администрацией.</p>
<p>1.2. Используя Сервис, включая запуск бота, регистрацию, оплату услуг или получение
доступа к материалам, Пользователь подтверждает, что полностью ознакомился с условиями
настоящего Соглашения и принимает их в полном объёме.</p>
<p>1.3. В случае несогласия с условиями Соглашения Пользователь обязан прекратить
использование Сервиса.</p>

<h2>2. Характер услуг и цифровых товаров</h2>
<p>2.1. Сервис предоставляет цифровые товары и услуги нематериального характера, включая,
но не ограничиваясь: информационные материалы, обучающие программы, консультации,
цифровые продукты и сервисные услуги.</p>
<p>2.2. Материалы, предоставляемые через Сервис, могут включать:</p>
<ul>
<li>информацию из открытых источников;</li>
<li>авторские материалы Администрации и/или третьих лиц;</li>
<li>аналитические обзоры, подборки, рекомендации, структурированные данные.</li>
</ul>
<p>2.3. Пользователь осознаёт и соглашается, что ценность цифровых товаров и услуг
Сервиса заключается в систематизации, анализе, форме подачи, сопровождении, поддержке
и обновлениях, а не в эксклюзивности отдельных фрагментов информации.</p>
<p>2.4. Сервис не заявляет и не гарантирует уникальность, исключительность или
недоступность отдельных элементов материалов вне Сервиса.</p>

<h2>3. Отказ от гарантий и ответственности</h2>
<p>3.1. Сервис предоставляется на условиях «AS IS» («как есть»).</p>
<p>3.2. Администрация не гарантирует:</p>
<ul>
<li>соответствие Сервиса ожиданиям Пользователя;</li>
<li>достижение каких-либо финансовых, коммерческих, профессиональных или иных результатов;</li>
<li>бесперебойную и безошибочную работу Сервиса.</li>
</ul>
<p>3.3. Администрация не несёт ответственности за:</p>
<ul>
<li>любые прямые или косвенные убытки, включая упущенную выгоду;</li>
<li>последствия применения Пользователем полученных материалов;</li>
<li>действия или бездействие третьих лиц;</li>
<li>временные технические сбои и ограничения доступа.</li>
</ul>
<p>3.4. Все решения о применении материалов, рекомендаций и услуг принимаются
Пользователем самостоятельно и на его риск.</p>

<h2>4. Законность использования</h2>
<p>4.1. Сервис не предназначен для поощрения, организации или содействия противоправной
деятельности.</p>
<p>4.2. Пользователь обязуется использовать Сервис исключительно в рамках применимого
законодательства и правил третьих сторон.</p>
<p>4.3. Ответственность за законность использования материалов и услуг Сервиса полностью
возлагается на Пользователя.</p>

<h2>5. Интеллектуальная собственность</h2>
<p>5.1. Все материалы, размещённые в Сервисе, охраняются законодательством об
интеллектуальной собственности.</p>
<p>5.2. Пользователю запрещается копировать, распространять, перепродавать, передавать
третьим лицам или иным образом использовать материалы Сервиса без разрешения
правообладателя.</p>
<p>5.3. Нарушение прав интеллектуальной собственности может повлечь ограничение доступа
к Сервису без компенсации.</p>

<h2>6. Ограничение доступа</h2>
<p>6.1. Администрация вправе приостановить или ограничить доступ Пользователя к Сервису
в случае:</p>
<ul>
<li>нарушения условий настоящего Соглашения;</li>
<li>выявления злоупотреблений;</li>
<li>требований законодательства или платёжных провайдеров.</li>
</ul>
<p>6.2. Ограничение доступа не освобождает Пользователя от обязательств, возникших ранее.</p>
<p>6.3. Администрация оставляет за собой право отказывать в обслуживании Пользователям,
чьи действия могут создавать повышенные риски для Сервиса, платёжных провайдеров или
третьих лиц.</p>

<h2>7. Платежи и возвраты</h2>
<p>7.1. Оплата услуг и цифровых товаров производится на условиях, указанных в Сервисе
до момента оплаты.</p>
<p>7.2. В связи с нематериальным характером цифровых товаров и услуг, возврат денежных
средств после предоставления доступа не осуществляется, за исключением случаев,
указанных ниже.</p>
<p>7.3. Возврат средств возможен только если:</p>
<ul>
<li>услуга не была оказана по технической вине Сервиса;</li>
<li>доступ к цифровому товару фактически не был предоставлен.</li>
</ul>
<p>7.4. Для рассмотрения вопроса о возврате Пользователь обязан обратиться в службу
поддержки в течение 24 часов с момента оплаты.</p>
<p>7.5. Решение о возврате принимается Администрацией индивидуально.</p>
<p>7.6. Пользователь подтверждает, что обязуется не инициировать возврат платежа
(chargeback) через платёжные системы без предварительного обращения в службу поддержки
Сервиса.</p>

<h2>8. Конфиденциальность</h2>
<p>8.1. Администрация может собирать минимально необходимые технические данные для
обеспечения работы Сервиса.</p>
<p>8.2. Администрация принимает разумные меры для защиты данных, однако не гарантирует
абсолютную безопасность передаваемой информации.</p>

<h2>9. Изменение условий</h2>
<p>9.1. Администрация вправе вносить изменения в настоящее Соглашение.</p>
<p>9.2. Актуальная версия Соглашения публикуется в Сервисе.</p>
<p>9.3. Продолжение использования Сервиса означает согласие Пользователя с обновлёнными
условиями.</p>

<h2>10. Контактная информация</h2>
<p>10.1. По всем вопросам Пользователь может обратиться в службу поддержки через форму
в самом боте.</p>

<p>Используя Сервис (в том числе запуская бота и/или вводя команду /start), Пользователь
подтверждает, что ознакомлен с настоящим Соглашением и принимает его условия в полном
объёме.</p>
"""

SUPPORT_TEXT = f"""
<h2>Связаться с нами</h2>
<p>Лучший способ — наш Telegram-бот: он всегда под рукой.</p>
<p>Также можно написать на почту: <b>support@pushkavpn.example</b></p>
<h2>Частые вопросы</h2>
<p><b>Сколько стоит?</b> — от {TARIFFS['lite']['price']} ₽ в день: Лайт
({TARIFFS['lite']['gb']} ГБ), Стандарт ({TARIFFS['standard']['gb']} ГБ),
Про ({TARIFFS['pro']['gb']} ГБ), Бизнес ({TARIFFS['business']['gb']} ГБ).</p>
<p><b>Как списываются деньги?</b> — раз в сутки суммой стоимости всех активных
устройств. Если баланса не хватает — устройства отключаются до пополнения.</p>
<p><b>Есть бесплатный тариф?</b> — да, пробный доступ {TRIAL_MB} МБ на {TRIAL_HOURS} час,
один раз на аккаунт.</p>
<p><b>Как подключить устройство?</b> — нажмите «➕ Подключить новое устройство»,
выберите тариф и отсканируйте QR-код.</p>
<p><b>Не пришла оплата</b> — напишите в поддержку, указав сумму и время платежа.</p>
"""


init_db()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
