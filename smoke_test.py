"""Смоук-тест PushkaVPN: маршруты, per-device тарифы, списание, документы, OAuth."""

import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_PATH", "/tmp/pushka_smoke2.db")
os.environ.setdefault("SITE_URL", "https://pushkavpn.example")
os.environ.setdefault("TELEGRAM_CLIENT_ID", "123456789")
os.environ.setdefault("TELEGRAM_CLIENT_SECRET", "test-secret")

if os.path.exists("/tmp/pushka_smoke2.db"):
    os.remove("/tmp/pushka_smoke2.db")

import app as appmod  # noqa: E402

app = appmod.app
client = app.test_client()
failures = []


def check(name, cond, extra=""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(name)


# --- публичные страницы ---
for route in ["/", "/rules", "/privacy", "/terms", "/login", "/register"]:
    r = client.get(route)
    check(f"GET {route} -> 200", r.status_code == 200, f"got {r.status_code}")
r = client.get("/support")
check("GET /support -> Telegram", r.status_code in (302, 301) and "t.me/Pushka_Sup" in r.headers.get("Location", ""))

# --- документы ---
priv = client.get("/privacy").get_data(as_text=True)
check("privacy: раздел 7", "7. Изменения в Политике" in priv)
check("privacy: нет артефакта", "[06.10.2026" not in priv)
check("privacy: дата", "1 октября 2026" in priv)

terms = client.get("/terms").get_data(as_text=True)
check("terms: раздел 10 + /start", "10.1." in terms and "вводя команду /start" in terms)
check("terms: chargeback", "chargeback" in terms)
check("terms: нет артефакта", "[06.10.2026" not in terms)

# --- футер на каждой странице ---
for route in ["/", "/login", "/terms"]:
    page = client.get(route).get_data(as_text=True)
    check(f"footer на {route}", 'href="/privacy"' in page and 'href="/terms"' in page)

# --- плейсхолдеры убраны, согласия на месте ---
reg = client.get("/register").get_data(as_text=True)
check("register: нет placeholder", "placeholder=" not in reg)
check("register: подсказка про 6 символов", "не короче 6 символов" in reg)
check("register: согласие", "Регистрируясь, вы принимаете" in reg)
check("register: кнопка Telegram OAuth", 'href="/auth/telegram"' in reg)

log = client.get("/login").get_data(as_text=True)
check("login: нет placeholder", "placeholder=" not in log)
check("login: кнопка Telegram OAuth", 'href="/auth/telegram"' in log)

# --- лендинг ---
index = client.get("/").get_data(as_text=True)
check("index: заголовок", "Лучший VPN" in index and "PushkaVPN" in index)
check("index: дешёвые тарифы", "Дешёвые тарифы" in index and "От 10₽" in index)
check("index: без старого hero", "Интернет без границ" not in index)
check("index: нет верхнего меню документов", ">Тарифы<" not in index)

# --- фон: осенний хэллоуин ---
css = client.get("/static/css/style.css").get_data(as_text=True)
check("css: чёрный фон", "#070a14" in css)
check("css: оранжевый акцент", "#ff7a18" in css)
check("css: фиолетовое свечение", "#6b2d8b" in css or "rgba(107, 45, 139" in css)
check("css: падающие листья", "leaf-fall" in css)
check("css: сетка из 4 тарифов", ".tariff-grid" in css)

# --- логотип и фавикон ---
check("logo.png есть", os.path.exists(os.path.join(appmod.BASE_DIR, "static", "img", "logo.png")))
check("favicon.png есть", os.path.exists(os.path.join(appmod.BASE_DIR, "static", "img", "favicon.png")))
check("pumpkin.png есть", os.path.exists(os.path.join(appmod.BASE_DIR, "static", "img", "pumpkin.png")))

# --- регистрация email ---
r = client.post(
    "/register",
    data={"email": "user@example.com", "password": "secret123", "password2": "secret123"},
)
check("POST /register -> 302", r.status_code == 302 and "/dashboard" in r.headers["Location"])

r = client.post("/login", data={"email": "user@example.com", "password": "secret123"})
check("POST /login -> dashboard", r.status_code == 302)

# --- кабинет ---
r = client.get("/dashboard")
check("GET /dashboard -> 200", r.status_code == 200)
dash = r.get_data(as_text=True)
check("dashboard: вкладки", all(t in dash for t in ["Главная", "Устройства", "Баланс", "Профиль", "Поддержка"]))
check("dashboard: плитка баланса", "Баланс" in dash and "Подключить устройство" in dash)
check("dashboard: промокод", "Промокод" in dash)
check("dashboard: промокод в канале", "Telegram-канале" in dash)

# --- страница пополнения ---
r = client.get("/topup")
check("GET /topup -> баланс", r.status_code in (200, 302))
if r.status_code == 302:
    r = client.get(r.headers.get("Location", "/dashboard/balance"))
check("GET /dashboard/balance -> 200", r.status_code == 200)
top = r.get_data(as_text=True)
check("topup: согласие", "Оплачивая, вы принимаете" in top)
check("topup: сумма от 100", 'min="100"' in top)
check("balance: история", "История платежей" in top)
check("balance: другу", "Пополнить другу" in top)

# --- триал ---
r = client.post("/trial")
check("trial: активация", r.status_code == 200 and r.get_json()["success"])
r = client.post("/trial")
check("trial: повтор -> ошибка", r.status_code == 402)

# --- API устройств (мок VPS) ---
real_post = appmod.requests.post


def fake_vps(url, *args, **kwargs):
    class R:
        status_code = 200

        ok = True

        def json(self):
            return {"success": True, "link": "vless://abc-" + str(fake_vps.n)}

    fake_vps.n += 1
    return R()


fake_vps.n = 0
appmod.requests.post = fake_vps

# подключение устройства с тарифом lite
r = client.post("/device", json={"tariff": "lite"})
body = r.get_json() or {}
check("POST /device (lite) -> 200", r.status_code == 200 and body.get("success"), r.get_data(as_text=True))
check("device: vless:// ссылка", str(body.get("link") or "").startswith("vless://"))

# лимит lite = 1 устройство: второе должно быть отклонено (409)
r = client.post("/device", json={"tariff": "lite"})
check("второе lite -> 409 (лимит 1 устройство)", r.status_code == 409, f"got {r.status_code}")

# стандарт (лимит 2) — ок
r = client.post("/device", json={"tariff": "standard"})
check("POST /device (standard) -> 200", r.status_code == 200)

appmod.requests.post = real_post

# список устройств в кабинете
dash = client.get("/dashboard/devices").get_data(as_text=True)
check("dashboard: устройства в таблице", "Устройство 1" in dash and "tariff-select" in dash)
check("devices: заголовок", "Ваши устройства" in dash)

# --- смена тарифа устройства ---
# достаём id устройств из БД
import sqlite3  # noqa: E402

con = sqlite3.connect("/tmp/pushka_smoke2.db")
con.row_factory = sqlite3.Row
devs = con.execute("SELECT * FROM devices ORDER BY id").fetchall()
check("в БД 2 устройства", len(devs) == 2)
dev1_id = devs[0]["id"]

# переводим первое устройство с lite на pro — lite освобождается, pro (лимит 3) ок
r = client.post(f"/device/{dev1_id}/tariff", json={"tariff": "pro"})
check("смена lite -> pro", r.status_code == 200 and r.get_json()["success"])

# неизвестный тариф
r = client.post(f"/device/{dev1_id}/tariff", json={"tariff": "gold"})
check("неизвестный тариф -> 400", r.status_code == 400)

# --- суточное списание ---
user = con.execute("SELECT * FROM users WHERE email = 'user@example.com'").fetchone()
check("баланс до = 0", user["balance"] == 0)

# начисляем баланс 1000₽ напрямую и поднимаем billing_day на вчера
con.execute("UPDATE users SET balance = 1000, billing_day = '2000-01-01' WHERE id = ?", (user["id"],))
con.commit()

r = client.get("/dashboard")  # ensure_billing внутри
user = con.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
# pro (50) + standard (25) = 75₽
check("списано 75₽ (pro+standard)", abs(user["balance"] - 925) < 0.01,
      f"balance={user['balance']}")
check("billing_day = сегодня", user["billing_day"] == appmod.today())

# повторный заход в тот же день не списывает повторно
r = client.get("/dashboard")
user = con.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
check("повторное списание не прошло", abs(user["balance"] - 925) < 0.01)

# --- трафик: лимит тарифа на устройство (устройства ещё активны) ---
r = client.post(
    "/usage",
    json={"user_id": user["id"], "mb": 5000, "device_id": dev1_id},
    headers={"X-Secret": appmod.VPS_SECRET},
)
# pro = 40 ГБ = 40960 МБ, 5000 влезает
check("usage pro 5000МБ -> active", r.status_code == 200 and r.get_json()["active"],
      r.get_data(as_text=True))

# превышение лимита pro
r = client.post(
    "/usage",
    json={"user_id": user["id"], "mb": 50000, "device_id": dev1_id},
    headers={"X-Secret": appmod.VPS_SECRET},
)
check("превышение лимита -> не active", not r.get_json()["active"], r.get_data(as_text=True))

# нехватка баланса -> все устройства отключаются
con.execute("UPDATE users SET balance = 10, billing_day = '2000-01-01' WHERE id = ?", (user["id"],))
con.commit()
r = client.get("/dashboard")
susp = con.execute("SELECT COUNT(*) AS c FROM devices WHERE user_id = ? AND suspended = 1",
                   (user["id"],)).fetchone()["c"]
check("все устройства отключены при нехватке", susp == 2, f"suspended={susp}")

# --- пополнение: бонус за первое пополнение ---
r = client.post("/pay", json={"amount": 50})
check("pay: сумма < 100 -> 400", r.status_code == 400)

appmod.requests.post = fake_vps  # мокаем Platega
r = client.post("/pay", json={"amount": 150})
appmod.requests.post = real_post
check("pay: 150₽ -> успех (мок)", r.status_code == 200 and r.get_json().get("success"),
      r.get_data(as_text=True))

# колбэк Platega: без бонуса за первое пополнение
r = client.post(
    "/platega/callback",
    json={"status": "CONFIRMED", "amount": 150, "transactionId": "tx-test-1",
          "description": f"Пополнение баланса #{user['id']}"},
)
check("callback: успех", r.status_code == 200 and r.get_json().get("success"))
user = con.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
check("баланс = 160 (10 + 150, без бонуса)", abs(user["balance"] - 160) < 0.01,
      f"balance={user['balance']}")

# устройства снова включены
susp = con.execute("SELECT COUNT(*) AS c FROM devices WHERE user_id = ? AND suspended = 1",
                   (user["id"],)).fetchone()["c"]
check("после пополнения устройства включены", susp == 0, f"suspended={susp}")

# повторный callback — дубликат
r = client.post(
    "/platega/callback",
    json={"status": "CONFIRMED", "amount": 150, "transactionId": "tx-test-1",
          "description": f"Пополнение баланса #{user['id']}"},
)
check("callback: дубликат не начисляет", r.get_json().get("duplicate"))
user = con.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
check("баланс не изменился", abs(user["balance"] - 160) < 0.01)

# промокод Pushka2026
r = client.post("/promo", json={"code": "Pushka2026"})
check("promo PUSHKA2026", r.status_code == 200 and r.get_json().get("bonus") == 100)
user = con.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
check("баланс после промо = 260", abs(user["balance"] - 260) < 0.01, f"balance={user['balance']}")
r = client.post("/promo", json={"code": "PUSHKA2026"})
check("повтор промо -> 409", r.status_code == 409)

# подарок другу: минимум 100
r = client.post("/gift", json={"to": "nobody", "amount": 50})
check("gift < 100 -> 400", r.status_code == 400)

# --- OAuth-редирект ---
c3 = app.test_client()
r = c3.get("/auth/telegram")
loc = r.headers.get("Location", "")
check("auth/telegram -> oauth.telegram.org",
      r.status_code == 302 and loc.startswith("https://oauth.telegram.org/auth?"))
check("OIDC: PKCE + state + scope",
      "code_challenge=" in loc and "state=" in loc and "scope=openid+profile" in loc)

r = c3.get("/auth/telegram/callback?code=x&state=wrong")
check("callback: неверный state -> /login", r.status_code == 302 and "/login" in r.headers["Location"])

# --- служебные API сохранены ---
c4 = app.test_client()
check("POST /pay без входа -> 401", c4.post("/pay", json={"amount": 100}).status_code == 401)
check("POST /trial без входа -> 401", c4.post("/trial").status_code == 401)
check("POST /device без входа -> 401", c4.post("/device", json={}).status_code == 401)
check("POST /usage без секрета -> 401",
      c4.post("/usage", json={"user_id": 1, "mb": 1}).status_code == 401)
check("POST /platega/callback -> 200", c4.post("/platega/callback", json={"status": "NEW"}).status_code == 200)
check("POST /auth/telegram/start -> 200", c4.post("/auth/telegram/start").status_code == 200)
check("POST /auth/telegram/confirm без секрета -> 401",
      c4.post("/auth/telegram/confirm", json={}).status_code == 401)
check("GET /auth/telegram/status -> 200", c4.get("/auth/telegram/status?token=x").status_code == 200)
check("/dashboard без входа -> /login", c4.get("/dashboard").status_code == 302)
check("/topup без входа -> /login", c4.get("/topup").status_code == 302)

# --- env и техтребования ---
src = open("app.py", encoding="utf-8").read()
for var in ["BOT_TOKEN", "VPS_API_URL", "VPS_SECRET", "DATABASE_PATH", "SECRET_KEY",
            "SITE_URL", "PLATEGA_MERCHANT_ID", "PLATEGA_SECRET",
            "TELEGRAM_CLIENT_ID", "TELEGRAM_CLIENT_SECRET"]:
    check(f"env: {var}", var in src)
check("DATABASE_PATH default /tmp/pushka.db",
      'os.environ.get("DATABASE_PATH", "/tmp/pushka.db")' in src)
check("app = Flask(__name__)", "app = Flask(" in src and "static_folder" in src)

# --- названия тарифов из ТЗ ---
for name in ["Лайт", "Стандарт", "Про", "Бизнес"]:
    check(f"тариф в коде: {name}", f'"{name}"' in src)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
