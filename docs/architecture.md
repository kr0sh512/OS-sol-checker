# Архитектура

## Статус и границы

Это спецификация и структура будущей системы, не работающий сервис. Исходники содержат только описания назначения; Dockerfile, Compose и профили nsjail — заготовки. Нет серверных обработчиков, миграций, запуска кода, установленных зависимостей или готовой защиты. Не принимать реальные посылки до выполнения критериев [плана реализации](implementation.md).

## Технологии и процессы

- Python 3.12+, FastAPI, Uvicorn, Jinja2; обычные HTML-страницы, небольшой JavaScript для отправки файла и опроса статуса. Отдельный SPA/Node.js не нужен.
- CommonMark через markdown-it-py, HTML-санитизация через nh3. HTML из задания не исполняется.
- PostgreSQL, psycopg 3: две базы с разными ролями и жизненными циклами — `checker_transient` и `checker_archive`. Дополнительный брокер очереди не нужен.
- Отдельный runner-контейнер с nsjail и GCC: новая песочница процессов на компиляцию и каждый тест. Новые контейнеры на посылку не создаются, доступа к Docker socket нет. Компиляция тоже обрабатывает недоверенный ввод.
- Docker Compose — основной способ описания и развёртывания системы: корневой `docker-compose.yml`. Один образ приложения для `web`, `worker`, `catalog-sync`; отдельный образ `runner` включает компилятор и два изолированных rootfs. Версии зависимостей, nsjail и образы фиксируются при реализации.

```mermaid
flowchart LR
    Browser[Браузер] --> Web[Web / API]
    Web --> Transient[(Временная БД и очередь)]
    Git[OS-tasks] --> Sync[Синхронизация каталога]
    Sync --> Catalog[Неизменяемый снимок заданий]
    Web --> Catalog
    Worker[Worker / планировщик] --> Transient
    Worker --> Catalog
    Worker --> Archive[(Постоянный архив)]
    Worker --> Runner[Runner: внутренний HTTP API]
    Runner --> Compile[nsjail: компиляция]
    Runner --> Run[nsjail: один тест]
```

## Границы модулей

| Каталог / файл | Ответственность |
| --- | --- |
| `src/os_sol_checker/domain/` | Типы задания, посылки и результата; состояния и чистые политики идентичности/сравнения. Без HTTP, SQL и инфраструктурных SDK. |
| `application/catalog.py` | Публикация и чтение согласованного каталога. |
| `application/submissions.py` | Приём посылки, проверка кэша, создание записи и задания очереди. |
| `application/judging.py` | Компиляция, последовательные тесты, первый сбой, терминальный результат. |
| `application/archive.py` | Идемпотентное сохранение успешного исходника и факта успеха. |
| `application/sessions.py` | Имя, группа, принадлежность посылок и последняя посылка по задаче. |
| `ports/` | Контракты каталога, временного хранилища, архива, очереди и песочницы. |
| `adapters/catalog/` | Безопасное получение Git-снимка, парсинг структуры/Markdown и отпечаток tests. |
| `adapters/postgres/` | Реализации хранилищ, транзакций и очереди. |
| `adapters/runner/` | Клиент внутреннего HTTP API runner, ограниченный транспорт и удалённый cleanup. |
| `worker/` | Единственный активный планировщик, слоты, восстановление после сбоев. |
| `runner/` | Внутренний API, авторизация worker, N локальных слотов, watchdog и адаптер nsjail. Не читает БД или каталог тестов. |
| `web/` | HTTP, cookies, CSRF, шаблоны, статические ресурсы; без запуска процессов. |
| `sandbox/` | Изолированные compile/run stage, не имеющие доступа к управляющему API runner. |
| `migrations/transient/`, `migrations/archive/` | Независимые миграции двух баз. |
| `docker-compose.yml`, `deploy/` | Основное описание сервисов, Dockerfile, профили nsjail и защиты контейнеров; не бизнес-логика. |

Не создавать отдельный микросервис для каждого модуля. Зависимости направлены из адаптеров в application/domain; домен не импортирует адаптеры. Интерфейсы выделены там, где пользователь ожидает смену алгоритма: `SourceIdentity`, `TestsFingerprint`, `OutputComparator`, `AcceptedSolutionStore`, `SandboxBackend`.

## Контракты портов

Имена ниже — будущие публичные интерфейсы, а не уже существующие символы. DTO определяются в `domain/models.py`; I/O-порты асинхронные, чистые политики синхронные.

| Порт | Операции и инварианты |
| --- | --- |
| `CatalogRepository` | `current()`, `get_task(snapshot_id, task_id)`, `publish(snapshot)`; запрос получает один неизменяемый снимок целиком. |
| `SourceIdentity` | `identify(raw_bytes) -> (sha256, byte_size)`; исходные байты не нормализуются. Другая эвристика позднее не меняет формат хранимого оригинала. |
| `TestsFingerprint` | `calculate(tree) -> digest`; включает все каталоги tests, пути, типы записей и байты, а не mtimes. |
| `SubmissionStore` | Атомарный приём посылки + last-submission + запись очереди; смена поколения и чтение кэша транзакционно согласованы. |
| `ResultCache` | `get(cache_key)`, `put_if_current(generation, key, result)`; только окончательные результаты проверки, не инфраструктурные ошибки. |
| `Queue` | `claim()`, `finish()`, `invalidate_generation()`; очередь и посылка в одной временной БД. |
| `AcceptedSolutionStore` | `record_success(identity, source, task_id, context, submitter)`; атомарная уникальность blob и связи с задачей. |
| `OutputComparator` | `compare(expected_bytes, actual_bytes) -> match / bounded_diff`; начальная политика — строгое побайтовое равенство. |
| `SandboxBackend` | `open(submission_id, policy, remaining_seconds) -> handle`, `compile(handle, source) -> artifact / failure`, `run(handle, artifact, stdin) -> outcome`, `destroy(handle)`; runner сам ограничивает время и слоты даже при потере worker. |
| `SessionStore` | Случайная сессия, профиль, принадлежность посылок, last-submission; UUID посылки сам по себе не даёт доступ. |

`SandboxOutcome` содержит exit code/signal, причину принудительной остановки, wall time и ограниченные stdout/stderr. Нельзя определять причину остановки только по тексту stderr. Контекст проверки: commit каталога, tests digest, generation, runner image digest, sandbox profile digest (nsjail/rootfs/seccomp), flags digest, limits и comparator version. Он неизменяем после приёма; runner обязан подтвердить запрошенную политику.

Архив хранит исторический факт успеха в конкретном контексте, поэтому поздний commit старой проверки допустим только со старым контекстом и никогда не становится текущим результатом. Общая транзакция двух БД не нужна. `accepted` публикуется после подтверждения архива и повторной проверки generation в transient; смена tests не ждёт доступности архива.

Runner принимает только внутренние аутентифицированные запросы от worker. `RUNNER_URL=http://runner:8081`, порт не публикуется на хосте. `RUNNER_TOKEN` — обязательный секрет worker/runner, отсутствующий в web и дочерних jail. API runner: `POST /v1/executions` резервирует слот и фиксирует невозобновляемый deadline; `POST /v1/executions/{id}/compile` компилирует один исходник; `POST /v1/executions/{id}/run` запускает следующий тест; `GET /v1/executions` возвращает контроллеру активные handles для восстановления; `DELETE /v1/executions/{id}` идемпотентно прекращает процессы и удаляет данные. Артефакт остаётся внутри runner, клиент получает только непрозрачную ссылку. Точный транспорт и ошибки определены в [безопасности](security.md).

## Настройки по умолчанию

| Настройка | Значение | Смысл |
| --- | --- | --- |
| `MAX_SOURCE_BYTES` | 102400 | 100 КиБ; ровно это число разрешено. |
| `MAX_REQUEST_BYTES` | 131072 | Весь multipart body, включая служебные поля. |
| `MAX_CONCURRENT_SANDBOXES` | 4 | Глобальный предел, не на реплику API. |
| `QUEUE_CAPACITY` | 1000 | Максимум ожидающих уникальных jobs; followers не занимают новое место. |
| `COMPILE_TIMEOUT_SECONDS` | 10 | Wall time процесса компиляции. |
| `RUN_TIMEOUT_SECONDS` | 2 | Wall time одного теста. |
| `SUBMISSION_DEADLINE_SECONDS` | 60 | Активное выполнение, создание и уборка jail; ожидание очереди не входит. |
| `SANDBOX_MEMORY_MIB` | 256 | Предел памяти всего дерева процессов одного stage. |
| `SANDBOX_PIDS_LIMIT` | 64 | Предел процессов всего sandbox cgroup, не каждого процесса отдельно. |
| `SANDBOX_TMP_MIB` | 32 | Ограниченный memory-backed scratch. |
| `MAX_OUTPUT_BYTES` | 65536 | Общий stdout + stderr одного stage; превышение останавливает stage. |
| `MAX_ARTIFACT_BYTES` | 1048576 | Предел скомпилированного файла; отдельный бинарный канал. |
| `TRANSIENT_TTL_HOURS` | 24 | Срок хранения терминальных посылок и их исходников. |
| `SESSION_TTL_DAYS` | 30 | Профиль и ссылки на последние посылки. |
| `CATALOG_POLL_SECONDS` | 60 | Период проверки upstream. |

Изменение влияющих на проверку настроек создаёт новый `judge_policy_digest`; старые кэш-результаты не подходят. Проверять настройки при старте: положительные значения, compile/run timeout не выше общего deadline, общий deadline не выше 60 секунд. Секреты не имеют значений по умолчанию.

Транспорт начальной политики рассчитан на `0 < MAX_SOURCE_BYTES <= 102400`, `0 < MAX_OUTPUT_BYTES <= 65536` и `0 < MAX_ARTIFACT_BYTES <= 1048576`. Превышение этих верхних границ — ошибка конфигурации, а не неявное усечение данных. Меньшие лимиты проверяются обеими сторонами по подтверждённой политике.

`PUBLIC_ORIGIN` (точный HTTPS origin интерфейса), `CSRF_SECRET`, `RUNNER_TOKEN`, обе database URL и `RUNNER_IMAGE` (неизменяемая ссылка по digest) обязательны без defaults. Секреты доступны только нужным сервисам и не попадают в jail. Число runner-реплик — строго одно: глобальный предел обеспечивают worker и авторитетный локальный семафор runner. Поддерживаемый Linux-хост, cgroup delegation и проверенные права описаны в [развёртывании](deployment.md).

## Связанные спецификации

- [Формат каталога, Markdown и обновления](catalog.md).
- [Посылки, БД, cookies, HTTP и интерфейс](submissions.md).
- [Песочница, ограничения и развёртывание](security.md).
- [Порядок реализации и проверка готовности](implementation.md).
- [Docker Compose и требования к Linux-хосту](deployment.md).
