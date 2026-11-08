# OS-sol-checker

Проверка C-решений задач по операционным системам. Условия, подсказки и пары тестов берутся из [OS-tasks](https://github.com/kr0sh512/OS-tasks); решения компилирует и запускает отдельный runner с nsjail. Web и worker не исполняют пользовательский код.

## Компоненты

- FastAPI/Jinja2: темы, задачи, безопасный Markdown, независимые закрытые подсказки, профиль и отправка `.c`.
- `catalog-sync`: немедленный импорт и периодический HTTPS Git fetch, immutable snapshots, глобальный tests fingerprint и атомарная публикация.
- Временная PostgreSQL: cookie-сессии, все принятые отправки, очередь, кэш и generation/policy fencing.
- Worker: один активный планировщик, N параллельных executions, остановка на первом непрошедшем тесте, независимая доставка успехов в архив.
- Runner: один отдельный контейнер, новые nsjail для компиляции и каждого теста, собственные слоты и watchdog. Нет Docker socket и контейнеров на каждую отправку.
- Постоянная PostgreSQL: исходные успешные коды BYTEA по SHA-256 + размеру, связи с задачей/набором тестов/политикой и индивидуальные квитанции отправок.

Каждая принятая отправка получает собственный UUID, включая cache hit и follower. Кэш не раскрывает чужие имена или исходники. При смене tests результаты инвалидируются, старые исполнения отменяются; профиль и постоянный архив сохраняются. Профиль не удостоверяет реальную личность.

## Локальная разработка

Требуются Python 3.12+, [uv](https://docs.astral.sh/uv/) **0.13.5 или совместимый с lock revision 5**, Git и две PostgreSQL-базы. Для исполнения дополнительно нужен поддерживаемый Linux-хост с настроенным nsjail runner; обычный subprocess на машине разработчика не является заменой.

```sh
uv sync --frozen --extra dev
cp .env.example .env
```

Для выбранной роли заполнить только необходимые переменные; не передавать web пароль архива или runner token. `CATALOG_ROOT` — общий каталог immutable snapshots для sync/web/worker. Секреты и `var/` исключены из VCS.

### Миграции

Миграции запускаются отдельно с owner DSN. Для production runtime используются отдельные LOGIN-роли; bootstrap контейнерной поставки создаёт их заранее.

```sh
export TRANSIENT_MIGRATION_DATABASE_URL='postgresql://OWNER:PASS@HOST/checker_transient'
export ARCHIVE_MIGRATION_DATABASE_URL='postgresql://OWNER:PASS@HOST/checker_archive'
uv run checker-migrate --web-role checker_web --worker-role checker_worker \
  --catalog-role checker_catalog --archive-role checker_archive_runtime
```

Для частного development-окружения без созданных ролей можно выполнить `uv run checker-migrate`: явные owner DSN берутся из переменных выше либо из `TRANSIENT_DATABASE_URL`/`ARCHIVE_DATABASE_URL`. Использование owner-роли самим приложением не является рекомендуемой production-конфигурацией.

### Каталог

```sh
uv run checker-catalog --once
uv run checker-catalog
```

Вторая команда выполняет первый импорт сразу и затем повторяет синхронизацию каждые `CATALOG_POLL_SECONDS` (по умолчанию 60). `--ref` позволяет зафиксировать нужную ревизию. Ошибка fetch/импорта оставляет последний валидный снимок; одновременно работает один publisher. Первый запуск без валидного снимка не объявляет систему готовой.

### Приложение

В отдельных процессах с соответствующими настройками:

```sh
uv run checker-runner
uv run checker-worker
uv run checker-web --host 127.0.0.1 --port 8000
```

Runner запускается только в подготовленном исполнительном контейнере. Worker получает проверенную политику из его внутреннего API и публикует её в БД. Web требует точный `PUBLIC_ORIGIN=https://...` и `CSRF_SECRET`; внешний HTTPS reverse proxy направляет запросы на локальный backend. Secure-cookie не следует отключать для обхода настройки HTTPS.

`GET /health/live` проверяет web-процесс. `GET /health/ready` дополнительно проверяет БД, каталог, активную политику и heartbeat планировщика; heartbeat выдаётся при доступных обязательных зависимостях. Готовность HTTP не заменяет испытание изоляции.

## Docker Compose

Основной файл — [docker-compose.yml](docker-compose.yml). Сборка образов, bootstrap role-scoped secret files, делегирование cgroups, загрузка защитных профилей, immutable image digests и запуск описаны в [docs/deployment.md](docs/deployment.md).

У всех сервисов раздельные сети и credentials. Порты runner и БД не публикуются. `deploy/secrets/` исключён и из VCS, и из контекста сборки. Секреты не входят в jail.

**Не запускать недоверенные посылки на неподготовленном хосте.** Runner обязан отказывать при неподходящих namespaces/cgroups/security profiles. Rootful Linux и необходимые привилегии не возникают автоматически от наличия Compose-файла. Не включать `privileged: true`, unconfined-профили или доступ к Docker socket как обход ошибки запуска.

## Проверки

```sh
uv run pytest
uv run ruff check src tests
```

Для PostgreSQL-регрессий указать две owner DSN **в отдельных тестовых базах**:

```sh
export TEST_TRANSIENT_DATABASE_URL='postgresql://OWNER:PASS@HOST/checker_test_transient'
export TEST_ARCHIVE_DATABASE_URL='postgresql://OWNER:PASS@HOST/checker_test_archive'
uv run pytest tests/integration/test_postgres.py
```

Каждый PostgreSQL-тест создаёт и удаляет собственные схемы; проверка ролей дополнительно требует `CREATEROLE`. Без DSN соответствующие проверки пропускаются. Для реального runner smoke обязательны поддерживаемый runtime и делегирование; тесты с моделированными I/O-границами не доказывают изоляцию.

Проектные пределы: файл до **102400 байт включительно**, N=4, компиляция 10 секунд, тест 2 секунды, общий активный бюджет до 60 секунд. Физическая остановка при зависшем ядре/замороженном хосте не может быть безусловно гарантирована. До подтверждённой уборки слот не освобождается.

## Структура и контракты

| Документ | Содержание |
| --- | --- |
| [Архитектура](docs/architecture.md) | Процессы, порты, модели и настройки. |
| [Каталог](docs/catalog.md) | Git, формат задач, Markdown и tests fingerprint. |
| [Посылки](docs/submissions.md) | Хранилища, кэш, cookies, HTTP и состояния интерфейса. |
| [Безопасность](docs/security.md) | nsjail, внутренний API, cgroups, watchdog и cleanup. |
| [Развёртывание](docs/deployment.md) | Сборка, секреты, подготовка хоста и Compose. |
| [Приёмка](docs/implementation.md) | Проверяемые сценарии и границы эксплуатационной готовности. |
| [AGENTS.md](AGENTS.md) | Инварианты разработки и отдельные Jujutsu workspaces. |

Система контроля версий — **Jujutsu**. Независимые части разрабатываются в отдельных workspace; их изменения объединяются без переписывания соседних workspace. Использовать `jj status`, `jj diff`, `jj describe`, `jj log`; не смешивать это с `git add/commit`.
