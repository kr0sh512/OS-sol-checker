# Развёртывание на одном Linux-хосте

## Статус

Проект содержит спецификацию и заготовки, а не готовое приложение. Корневой [`docker-compose.yml`](../docker-compose.yml) намеренно состоит только из комментариев: он **НЕ ПРИГОДЕН ДЛЯ ЗАПУСКА**. Dockerfile, конфигурации nsjail и внешние security profiles также ещё не реализуют описанные гарантии. Сейчас команды `docker compose config` и `docker compose up` не являются поддерживаемым способом проверить или запустить проект.

Будущая основная поставка — Docker Compose на одном проверенном Linux-хосте: один образ приложения для web/worker/catalog-sync, отдельный образ runner и два независимых PostgreSQL-сервиса. Это целевой контракт для реализации, не обещание запуска неизменённой командой `docker run` на произвольной машине. Политика изоляции и внутренний протокол определены в [security.md](security.md), прикладные инварианты — в [architecture.md](architecture.md).

## Сервисы и доступ

| Сервис | Образ / процесс | Сети и права |
| --- | --- | --- |
| `web` | Общий application image; web/API. | Frontend и transient DB; read-only каталог. Только пользовательский HTTP-интерфейс, без runner token и archive credentials. |
| `worker` | Тот же application image; singleton scheduler. | Transient DB, archive DB и runner; read-only каталог. Единственный клиент runner, единственный писатель успешных решений в архив. |
| `catalog-sync` | Тот же application image; синхронизация снимков. | Transient DB и отдельный исходящий доступ к GitHub. Единственный писатель каталога; без runner/архива. |
| `runner` | Отдельный immutable runner image с GCC, nsjail и compile/run rootfs. | Только внутренняя сеть worker–runner. Не подключён к БД, каталогу или frontend; приватный scratch. Строго один экземпляр. |
| `postgres-transient` | Зафиксированный PostgreSQL image. | Только внутренняя transient DB сеть; временная БД/очередь/сессии. Нет публичного порта. |
| `postgres-archive` | Зафиксированный PostgreSQL image. | Только внутренняя archive DB сеть с worker; постоянные успешные исходники и факты успеха. Нет публичного порта. |

Application image собирается один раз из `deploy/containers/application.Dockerfile`; роли различаются будущими командами запуска, а не копиями образа. Runner собирается из `deploy/containers/runner.Dockerfile`; GCC, nsjail, compile/run rootfs и профили находятся внутри этого образа. Контейнеры на отдельную посылку не создаются; Docker socket не монтируется ни в один сервис.

Предполагаемые сети Compose:

- `frontend`: только web из сервисов приложения. Публичный HTTPS завершается на настроенном оператором reverse proxy; backend-порт web доступен ему, но не открывается миру в обход HTTPS/body limits. Если proxy находится на хосте, backend привязывается только к loopback хоста.
- `db-transient`, внутренняя: web, worker, catalog-sync и postgres-transient.
- `db-archive`, внутренняя: только worker и postgres-archive.
- `worker-runner`, внутренняя: только worker и runner; `RUNNER_URL=http://runner:8081`. Нет `ports` для runner; не подключать к общей сети приложения.
- `catalog-egress`: только catalog-sync из сервисов приложения, для HTTPS-доступа к фиксированному upstream на GitHub. Пользователь не задаёт URL репозитория. DNS/egress политика хоста должна учитывать реальные GitHub endpoints и не предоставлять доступ к внутренним адресам/metadata.

Изолированные сети уменьшают доступность сервисов, но сами по себе не являются полной egress-политикой: запрет нежелательных исходящих соединений требует проверенных правил хоста. Jail не получает даже сеть внешнего runner. Plaintext bearer разрешён только внутри выделенной сети одного доверенного хоста; перенос worker/runner на разные хосты либо в недоверенную сеть требует TLS с аутентификацией сторон и проверки peer identity. Простая замена URL на удалённый HTTP недопустима.

## Хранилища и секреты

| Хранилище | Владелец и жизненный цикл |
| --- | --- |
| Immutable snapshots каталога | Отдельный volume; catalog-sync монтирует на запись, web/worker только read-only. Публикация атомарная, снимок активной проверки сохраняется до завершения читателей. Runner volume не получает. |
| Данные postgres-transient | Ограниченный ephemeral tmpfs, без backup. Не использовать постоянный volume под видом временной БД. Пересоздание/перезапуск может потерять очередь, результаты и сессии; интерфейс сообщает потерю честно. |
| Данные postgres-archive | Отдельный persistent volume, с политикой backup и проверенным восстановлением. Не разделяет PostgreSQL data directory с transient. |
| Runner scratch | Приватный ограниченный tmpfs внешнего runner; только N ограниченных артефактов и служебные данные. Не shared volume с worker или другим сервисом. |
| Scratch стадии | Отдельные ограниченные tmpfs и cgroup для compile и каждого run; уничтожаются после стадии. Нет общего writable mount между jail. |

Ограничение tmpfs обязательно дополняется memory limit контейнера и стадий. PostgreSQL runtime directories, журналы и временные файлы также учитываются в ресурсных бюджетах; ephemeral-хранилище не должно неограниченно расходовать память хоста.

Не передавать один общий набор credentials всем сервисам. Для web, worker и catalog-sync нужны отдельные роли/секреты transient DB с минимальными правами согласно операциям. Только worker получает архивную прикладную роль; bootstrap/migration credentials не принадлежат обычному web-процессу. Пароли двух БД не совпадают. Секреты поступают через защищённые оператором файлы/механизм secrets; общее подключение всего `.env` ко всем контейнерам запрещено. Конкретные роли и выдача прав являются частью будущих миграций.

`RUNNER_TOKEN` — обязательный общий секрет только worker и runner; в примере конфигурации значение пустое, старта с пустым значением нет. Токен не передаётся в jail, не попадает в image layers и логи. `RUNNER_IMAGE` также оставляется пустым в примере: оператор обязан указать immutable reference по digest, а не изменяемый tag. `RUNNER_URL=http://runner:8081` — внутренний адрес, не публичный endpoint. `PUBLIC_ORIGIN`, `CSRF_SECRET` и обе прикладные database URL обязательны без встроенных секретных defaults. CSRF-секрет получает только нужный web-процесс.

Контекст каждой проверки фиксирует `runner_image_digest`, `sandbox_profile_digest`, флаги, лимиты и comparator. Запуск с несовпадающей политикой запрещён; обновление образа/профилей не должно незаметно переиспользовать старый кэш. Profiles внутри image и внешние защитные profiles хоста версионируются и испытываются вместе.

## Предпосылки Linux-хоста

Опорная среда реализации — выделенный Linux-хост, rootful Docker Engine, Docker Compose plugin и cgroup v2. Поддержка иных режимов Docker, настольных виртуализированных окружений или иных ОС этим документом не заявляется. Версии ядра/Engine/Compose/nsjail и допустимая конфигурация хоста должны быть зафиксированы по результатам испытаний, а не придуманы в заготовке.

До допуска посылок оператору и реализации требуется:

1. Проверить поддержку network/user/PID/mount/IPC/UTS namespaces, seccomp и выбранного внешнего профиля AppArmor. Политика хоста должна разрешать необходимые user namespace mappings и безопасное создание дочерних namespaces; невозможность этого — неготовность, не повод отключить изоляцию.
2. Подготовить **явно делегированное поддерево cgroup v2** с контроллерами memory/pids/cpu и работоспособным `cgroup.kill`. Делегирование, владельцы, границы видимости, настройка controllers и уборка после остановки требуют отдельной проверенной host provisioning процедуры/скрипта при реализации; сейчас такого готового скрипта нет. Runner видит только своё делегированное поддерево, не получает writable bind всего `/sys/fs/cgroup` хоста и не может менять ограничения родителя или соседей.
3. Подобрать и испытать минимальные права внешнего runner для namespaces, mounts и делегирования. Rootful Docker не означает, что процесс можно бездумно делать всесильным: capabilities и UID/GID внешнего runner определяются на опорном хосте и фиксируются в конфигурации. Не устанавливать `privileged: true`, `seccomp=unconfined` или отключение AppArmor как defaults. Если работа требует ослабления внешнего профиля, допускаются лишь конкретные проверенные разрешения, не blanket-разрешение всего.
4. Установить проверенные внешние profiles из будущих `deploy/security/runner-seccomp.json` и `deploy/security/runner-apparmor.profile`; отдельно применять внутренние compile/run profiles `deploy/nsjail/compile.cfg` и `deploy/nsjail/run.cfg`. Сейчас seccomp JSON намеренно пуст, остальные файлы содержат комментарии; ни один из них не пригоден к загрузке security policies. Нельзя выдавать наличие файла за enforcement.
5. Обеспечить внешний private PID namespace runner и supervisor/watchdog вне дочерних jail. Supervisor прекращает все стадии при отказе доверенных компонентов; остановка внешнего контейнера не оставляет потомков. Host PID namespace не используется.
6. Рассчитать и задать реальные внешние memory/PID/CPU/tmpfs limits. Для runner бюджет включает `N ×` stage resources плюс доверенные процессы, приватные артефакты и временные копии. Начально N=4, stage memory=256 МиБ, pids=64, scratch=32 МиБ и CPU quota одного CPU; overhead рассчитывается и испытывается отдельно. Бюджеты БД, каталога и application оставляют резерв ОС. Ограничить размер журналов и свободное место постоянного архива.
7. Настроить HTTPS, firewall/egress, хранение секретов, backup архива и восстановление; закрыть публичный доступ к runner/БД. Web/worker/catalog-sync работают non-root, с read-only rootfs там, где возможно, минимальными writable scratch, `no_new_privs` и без ненужных capabilities. Внешние права runner не наследуются процессами решения.

Compose не способен сам по себе доказать правильность host delegation, профилей или убийства дерева процессов. Runner при старте и readiness обязан проверять необходимые свойства; отсутствие поддержки namespace/cgroup/профиля даёт readiness false и HTTP 503 без fallback.

## Единственность, запуск и перезапуски

Runner запускается строго в одном экземпляре; его авторитетный семафор допускает N=4 executions, каждый с максимум одной активной стадией. Горизонтальное размножение runner нарушает глобальный предел и не поддерживается. Активный worker также один, с PostgreSQL advisory lock на отдельном соединении; число процессов web не меняет число слотов. При потере lock worker прекращает claims и отменяет свои executions.

Перед claims после старта worker перечисляет активные handles runner и отменяет устаревшие/незавершённые исполнения; если transient DB потеряна — все найденные. При недоступном runner новые посылки остаются в очереди; внутри runner нет второй очереди, заполнение слотов даёт 429. Слот не освобождается до подтверждённого убийства дерева, reaping, снятия mounts и удаления временных данных. Неуспешный cleanup блокирует готовность.

Перезапуск runner уничтожает transient artifact handles и scratch. Worker завершает затронутые посылки как infrastructure_error, не повторяет compile/run вслепую. Обрыв HTTP не доказывает, что программа не запускалась; правила отмены и восстановления определены в [security.md](security.md). Если состояние исполнения осталось неизвестным, доступность жертвуется ради подтверждённой уборки. Watchdog самостоятельно соблюдает оставшийся deadline при потере worker; при замороженном ядре физическая остановка ровно к deadline не гарантируется.

Будущая процедура развёртывания после реализации:

1. Подготовить опорный хост и проверить provisioning/delegation/security profiles; выбрать и зафиксировать digests образов, секреты, лимиты и backup.
2. Заполнить настоящие Dockerfile, Compose services/networks/volumes, команды и минимальные readiness probes; реализовать приложение, миграции, runner и watchdog. Снять статус заготовки только после испытаний из [плана реализации](implementation.md).
3. Тогда `docker compose config` проверяет синтаксис и подстановки окончательной конфигурации. Это не проверка изоляции и не запуск приложения.
4. Тогда `docker compose up -d` запускает сервисы; применить независимые миграции двух БД до готовности соответствующих процессов. Worker не начинает claims до готовности БД, публикации согласованного каталога, готовности runner и завершения recovery. Одного порядка старта контейнеров недостаточно.
5. Подтвердить на опорном хосте startup/readiness, все лимиты, отсутствие сетевых/fd/файловых утечек, kill/reap/cleanup, отказы supervisor/worker и восстановление архива. Только после этого открывать приём решений.

Эти команды приведены как будущий порядок действий; текущие заготовки не являются исполняемым Compose deployment.

## Первичные источники

- [Docker Compose: services](https://docs.docker.com/reference/compose-file/services/).
- [Docker Compose: networks](https://docs.docker.com/reference/compose-file/networks/).
- [Docker: cgroup и ограничения ресурсов](https://docs.docker.com/engine/containers/resource_constraints/).
- [Docker: seccomp](https://docs.docker.com/engine/security/seccomp/).
- [nsjail README](https://github.com/google/nsjail).
- [Linux: cgroup v2 и делегирование](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).
