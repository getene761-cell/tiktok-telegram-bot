# tiktok-telegram-bot

Асинхронный Telegram-бот (aiogram 3.x) для управления автоматизацией TikTok через
Playwright. Мультиаккаунт, вход по cookies или логину/паролю, лайки, комментарии,
ответы на комментарии, циклы, и — главное — **ручное решение капчи через Telegram**:
скриншот уходит админу, ответ возвращается на страницу.

---

## Структура

```
tiktok-telegram-bot/
├── bot.py                 # точка входа, aiogram-бот, меню, настройки
├── tiktok_worker.py       # Playwright-логика: вход, лайки, комментарии, капча
├── captcha_solver.py      # отправка капчи в Telegram и ожидание ответа
├── config.py              # конфигурация, .env, пути, глобальный статус
├── accounts.example.json  # шаблон аккаунтов (копируется в accounts.json)
├── requirements.txt
├── .env.example
├── cookies/               # cookie-файлы аккаунтов
└── captcha_shots/         # история скриншотов капчи
```

---

## Установка в Termux (Android) — пошагово

```bash
# 1. Базовые пакеты
pkg update && pkg upgrade -y
pkg install -y python git

# 2. Chromium для Android (важно: это системный браузер, а не сборка Playwright)
pkg install -y x11-repo
pkg install -y chromium

# 3. Проект
git clone https://github.com/getene761-cell/tiktok-telegram-bot.git
cd tiktok-telegram-bot
# репозиторий приватный: если git просит пароль, подставь GitHub-токен
# (Settings -> Developer settings -> Personal access tokens, scope repo):
# git clone https://getene761-cell:<TOKEN>@github.com/getene761-cell/tiktok-telegram-bot.git

# 4. Python-зависимости
pip install --upgrade pip
pip install -r requirements.txt

# 5. Конфиг
cp .env.example .env
nano .env          # вставь BOT_TOKEN и ADMIN_ID

# 6. Аккаунты
cp accounts.example.json accounts.json
nano accounts.json # логины/пароли или cookies_file

# 7. Запуск
python bot.py
```

### Про `playwright install chromium` на Termux

Официальная команда `playwright install chromium` скачивает сборку под glibc —
на Android (bionic) она **не запустится**. Правильный путь:

1. Ставим системный браузер: `pkg install chromium`
2. В `.env` указываем путь:

```bash
BROWSER_EXECUTABLE=/data/data/com.termux/files/usr/bin/chromium
HEADLESS=1
```

3. Проверяем, что путь совпадает:

```bash
command -v chromium
```

Если хочется использовать именно сборку Playwright, нужен полноценный glibc-контейнер:

```bash
pkg install proot-distro
proot-distro install debian
proot-distro login debian
# дальше внутри Debian — обычная установка python + playwright install chromium
```

Тогда `.env` не трогаем (`BROWSER_EXECUTABLE` пустой), но `python bot.py` запускается
уже внутри proot.

### Мелочи, которые берегут время

* Держи Termux в wakelock, иначе Android усыпит процесс на середине цикла:
  `termux-wake-lock` (пакет `termux-api`).
* Запуск в фоне: `nohup python bot.py > /dev/null 2>&1 &` или `tmux`.
* Android убивает фоновые процессы — для длинных сессий лучше `tmux new -s tiktok`.

---

## Настройка Telegram

1. **Токен бота**: у [@BotFather](https://t.me/BotFather) → `/newbot` → имя → username
   → получаешь строку вида `123456789:AAH...`. Кладёшь в `.env` как `BOT_TOKEN`.
2. **Свой admin_id**: напиши [@userinfobot](https://t.me/userinfobot) — он пришлёт
   `Id: 123456789`. Это значение идёт в `ADMIN_ID`.
3. Открой своего бота, нажми **Start** (иначе бот не сможет написать первым).

---

## Аккаунты

`accounts.json`:

```json
{
  "accounts": [
    {
      "name": "acc1",
      "username": "login_or_email",
      "password": "password",
      "cookies_file": "cookies/acc1.json",
      "proxy": null,
      "enabled": true
    }
  ]
}
```

* `cookies_file` — путь относительно корня проекта. Поддерживаются:
  * `storage_state` Playwright (`{"cookies": [...]}`),
  * обычный JSON-список cookie,
  * Netscape `cookies.txt` (экспорт из браузерного расширения).
* `enabled: false` — аккаунт скрывается из меню.
* `proxy` — своя прокси на аккаунт, иначе берётся `PROXY` из `.env`.
* Если cookies валидны, вход по паролю не выполняется. После успешного входа
  cookies автоматически пересохраняются в `cookies_file`.

Резервный вариант без JSON — `acc.txt`, строка на аккаунт: `login:password`.

---

## Как работает капча через Telegram

1. Воркер видит капчу (`#captcha-verify-image`, контейнеры `*captcha*`, слайдер,
   либо текст «Verify to continue»).
2. Делает скриншот именно элемента капчи (fallback — весь экран), сохраняет копию
   в `captcha_shots/`.
3. Вызывает `captcha_solver.request_sync(...)` — мост из потока Playwright в event loop
   бота (`asyncio.run_coroutine_threadsafe`). Поток воркера блокируется, бот — нет.
4. Бот отправляет админу фото с подписью:
   * обычная капча — «Введи текст с картинки»;
   * слайдер — «Отправь число: на сколько пикселей сдвинуть ползунок»;
   * выбор картинок — inline-кнопки «Вариант 1…N».
5. Ответ админа (текст или callback) резолвит `asyncio.Future` в `captcha_solver`.
6. Воркер получает строку, вводит её в поле (`#captcha-verify-input` и fallbacks),
   жмёт submit/Enter, проверяет, что капча исчезла. До 3 попыток.
7. Нет ответа за `CAPTCHA_TIMEOUT` (по умолчанию 300 с) — бот сообщает об этом,
   автоматизация останавливается на этом шаге.

Никаких сторонних сервисов распознавания — только глаза админа.

---

## Меню бота

| Кнопка | Что делает |
|---|---|
| ▶️ Запустить автоматизацию | выбор аккаунта → запуск воркера |
| ⏹ Остановить | выставляет `threading.Event`, прерывает паузы и циклы |
| 📊 Статус | аккаунт, действие, прогресс, счётчики, последняя ошибка |
| 📁 Загрузить cookies | файл `.json`/`.txt` или текст → сохранение → привязка к аккаунту |
| ⚙️ Настройки | текст комментария (спинтаксис), задержки, циклы, URL, тумблеры действий |
| 📋 Логи | последние 25 строк `bot.log` |

Команды: `/start`, `/menu`, `/status`, `/logs`, `/stop`, `/help`.

---

## Настройки действий

* **Текст комментария** — многострочный: на каждый цикл берётся случайная строка.
  Спинтаксис: `{привет|хай|ку} бро` → один из вариантов.
* **Задержки** — `min-max` секунд между действиями, дробятся на шаги по 0.5 с,
  чтобы «Стоп» срабатывал мгновенно.
* **Циклы** — сколько видео обработать за запуск.
* **Режим URL** — один и тот же ролик, повторные циклы (накрутка комментариев).
* **Лайк / Комментарий / Ответ** — независимые тумблеры.

---

## Селекторы и их судьба

TikTok меняет разметку и добавляет антибот. Все селекторы собраны константами в
начале `tiktok_worker.py` (`LIKE_SELECTORS`, `COMMENT_INPUT_SELECTORS`, …) и
применяются как «первый видимый из списка». Если действие перестало срабатывать:

```bash
# в отладочном режиме с окном
HEADLESS=0 python bot.py
```

и поправь один список — остальная логика не трогается.

Stealth-слой минимальный и намеренно: `navigator.webdriver = undefined`, подмена
`languages/plugins/hardwareConcurrency`, заглушка `permissions.query`, флаг
`--disable-blink-features=AutomationControlled`, реальный UA. Этого хватает против
базовых проверок; серьёзные антибот-системы требуют резидентных прокси и
человеческого профиля браузера.

---

## Ошибки и что они значат

| В логе | Причина | Что делать |
|---|---|---|
| `вход не выполнен` | cookies протухли, пароль неверный, капча после логина | обнови cookies, проверь пароль |
| `аккаунт заблокирован` | «too many attempts», лимит действий | пауза на аккаунт, больше задержки, прокси |
| `таймаут Playwright` | медленная сеть / тяжёлая страница | увеличь `NAV_TIMEOUT` |
| `капча не пройдена за 3 попытки` | неверный ввод / новая капча | проверь `captcha_shots/`, введи точнее |
| `Executable doesn't exist` | не задан путь к chromium | `BROWSER_EXECUTABLE` в `.env` |

---

## Ограничения

* Автоматизация TikTok нарушает ToS платформы — блокировки аккаунтов это норма,
  а не баг. Задержки от 8 с, лимит комментариев на аккаунт, разные прокси.
* Скриншот капчи уходит в Telegram — не запускай на публичном боте.
* `accounts.json`, `cookies/`, `.env`, `bot.log` в `.gitignore`: секреты не уезжают в git.
