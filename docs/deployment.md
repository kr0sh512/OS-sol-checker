# Развёртывание на одном Linux-хосте

## Поставка и честный статус

В репозитории реализованы Dockerfiles, Compose, role bootstrap, host provisioning,
runner API, nsjail executor, watchdog и PID1 supervisor. Это не сертификат защиты.
Реальное развёртывание hs-hel и выполненные namespace/cgroup/AppArmor/security smoke
описаны в [операционном отчёте](operations-hs-hel.md), вместе с оставшимся риском
общего хоста. В исходном окружении разработки production smoke не выполнялся. Rootless Podman, отсутствие enforcing AppArmor и отсутствие nsjail на
хосте разработки не подтверждают работоспособность production sandbox. Образ сам
содержит nsjail; без требуемого runtime runner остаётся неготовым, native fallback нет.

Поддерживаемый профиль поставки: выделенный **Linux x86-64**, rootful Docker Engine
без Docker userns-remap, Compose plugin, systemd, cgroup v2 с memory/pids/cpu и `cgroup.kill` (ядро с этой
функцией), enforcing AppArmor и seccomp. Конкретные минимальные версии
kernel/Engine/Compose/AppArmor и минимальность capability набора должны быть
зафиксированы оператором после реальных испытаний; здесь они не выдуманы. Ядро,
Docker, nsjail и GCC должны своевременно обновляться с выпуском новой judge policy.
Нельзя включать публичный приём решений до описанной ниже приёмки.

## Сервисы и сети

Основное описание — [`docker-compose.yml`](../docker-compose.yml). Шесть постоянных
сервисов и одноразовый `migrate`, без Docker socket, privileged/unconfined defaults:

| Сервис | Сети | Секреты и данные |
| --- | --- | --- |
| web | frontend, db-transient | Только собственный transient DSN и CSRF; каталог read-only |
| worker | db-transient, db-archive, worker-runner | Собственные transient/archive DSN и runner bearer; каталог read-only |
| catalog-sync | db-transient, catalog-egress | Только собственный transient DSN; единственный writer каталога |
| runner | worker-runner (internal) | Только runner bearer; private tmpfs и отдельное делегированное cgroup subtree |
| postgres-transient | db-transient (internal) | Ограниченный ephemeral tmpfs, свои owner/role passwords |
| postgres-archive | db-archive (internal) | Постоянный archive-data volume, отдельные owner/runtime passwords |
| migrate (one-shot) | db-transient, db-archive | Только owner migration DSN; runtime сервисы их не получают |

Runner и БД не публикуют порты. Web публикуется **только** на `127.0.0.1:8000`;
HTTPS завершает reverse proxy оператора. Прокси обязан ограничивать HTTP body и
таймауты, передавать корректный публичный origin и не открывать обход HTTPS.
Compose-сети не являются полноценным firewall: оператор ограничивает catalog
HTTPS/DNS egress фиксированным GitHub upstream и закрывает metadata/internal
addresses. Межхостовой plaintext RUNNER_URL запрещён; текущая поставка рассчитана
на приватную сеть одного хоста, не на удалённый HTTP runner.

Application image содержит Git и CA certificates. Web/worker/catalog/migrate работают
UID/GID 10001, read-only root, no_new_privs, без capabilities. Catalog-sync имеет
1024 MiB (Git child использует собственный AS limit 512 MiB); web/worker/migrate —
384 MiB. Лимиты snapshots/retention дополнительно определяются каталогом, а не
одним memory budget контейнера.

## Сборка immutable образов

Из корня checkout (с созданным и закоммиченным `uv.lock`):

```sh
docker build -f deploy/containers/application.Dockerfile -t REGISTRY/checker-app:RELEASE .
docker build -f deploy/containers/runner.Dockerfile -t REGISTRY/checker-runner:RELEASE .
docker push REGISTRY/checker-app:RELEASE
docker push REGISTRY/checker-runner:RELEASE
# Взять именно RepoDigest опубликованных образов, а не локальный image ID:
docker image inspect REGISTRY/checker-app:RELEASE --format '{{json .RepoDigests}}'
docker image inspect REGISTRY/checker-runner:RELEASE --format '{{json .RepoDigests}}'
```

`REGISTRY` и `RELEASE` здесь — выбранные оператором registry и релиз, не значения,
которые следует буквально запускать. В защищённом deployment `.env` установить
`APPLICATION_IMAGE=...@sha256:...` и `RUNNER_IMAGE=...@sha256:...` из настоящих RepoDigests.
Compose использует эти ссылки; runner отклоняет изменяемый tag в RUNNER_IMAGE.
Сборка разрешена отдельно от production `compose up`; образ не должен меняться под
уже опубликованным digest. Base images Python 3.12.10 Bookworm / uv 0.13.5 и
PostgreSQL 17.5 Bookworm заданы версиями; для воспроизводимого release оператор
также фиксирует их registry digests в build provenance/своём deployment.

Оба Dockerfile выполняют `uv sync --frozen --no-dev --no-editable`; отсутствие или
несогласованность lockfile — ошибка сборки, не повод установить latest. Миграции
входят в application package. Runner собирает **nsjail 3.4 из exact commit
079d70dda4aa1edd9512cfd25ff1e47e316dc355**, включая закреплённый kafel submodule.
GCC 12/glibc берутся из одного build stage для совместимых compile/run rootfs.
`sandbox/build_rootfs.py` копирует allowlist компилятора, headers/startup/linker
objects и зависимостей; run rootfs не содержит compiler, shell, Python, package
manager, сертификаты, device nodes или секреты.

В финальном образе находятся `/usr/local/bin/nsjail`,
`/opt/checker/rootfs/{compile,run}`, `/opt/checker/deploy/nsjail` и
`/opt/checker/runner-manifest.json`. Manifest создаётся **из реально установленных
файлов**, а на старте пересчитывается с обнаружением extra/missing/tampered files.
Его fingerprint покрывает nsjail binary, оба rootfs/init/probe, nsjail configs и
внутренние seccomp policies. `GET /v1/policy` возвращает настоящую shared
JudgePolicy; worker публикует её в БД, web/catalog runner credentials не имеют.
RUNNER_IMAGE — доверенная операторская привязка к фактически запущенному RepoDigest;
контейнер не получает Docker socket для её проверки.

## Секреты и роли

Секреты не хранятся в VCS. На новом deployment:

```sh
sudo python3 deploy/bootstrap_secrets.py --public-origin https://checker.example.edu
```

Команда создаёт **только в пустом** `deploy/secrets/`, не печатает значения и ничего
не перезаписывает. Каталог 0700, файлы 0400. UID владельцев: application secrets
10001, PostgreSQL password secrets 999. Это существенно: локальные file-backed
Compose secrets используют bind mount и не гарантируют переназначение uid/mode
из YAML. Rootful Engine читает защищённые host paths; runtime читает только свои
смонтированные файлы. Секреты не передаются общим env_file всем сервисам.

Создаются `web_database_url`, `worker_database_url`, `catalog_database_url`,
`archive_database_url`, `transient_migration_database_url`,
`archive_migration_database_url`, `csrf_secret`, `runner_token` и шесть
`*_password` файлов: transient_owner, archive_owner, web, worker, catalog,
archive_worker. `runner_token` — случайный секрет больше 32 байт, только у worker
и runner. `deploy/container_entrypoint.py` загружает строго role-specific `_FILE`
секреты в существующие Settings/CLI environment и exec-заменяется реальным сервисом.

`deploy/postgres/*-init.sh` создают разные LOGIN роли `checker_web`,
`checker_worker`, `checker_catalog`, `checker_archive_runtime`, не объединяя их в
широкую общую runtime роль. `checker_owner` — bootstrap/migration superuser с
разными паролями в двух БД. `migrate` вызывает:

```text
checker-migrate --web-role checker_web --worker-role checker_worker
                --catalog-role checker_catalog --archive-role checker_archive_runtime
```

Миграции задают прикладные grants и отзывают public CREATE. Web не читает archive,
catalog не получает исходники/сессии, worker не публикует каталог. Runtime не
мигрирует автоматически и не получает owner DSN. После потери ephemeral
postgres-transient необходимо заново выполнить `docker compose run --rm migrate`
и затем перезапустить application services; heartbeat/readiness до этого должны
быть false. Архив не восстанавливает текущую временную очередь автоматически.
При rotation нельзя просто заменить secret file без согласованного ALTER ROLE и
перезапуска потребителей; bootstrap намеренно отказывается перезаписывать данные.

## Host provisioning и реальная accounting граница

На **хосте**, до старта runner:

```sh
sudo bash deploy/provision_runner_host.sh 4 256 64
```

Скрипт требует rootful Docker, активный AppArmor и cgroup2, загружает enforcing
`deploy/security/runner-apparmor.profile` и создаёт отдельный transient systemd
service `checker-delegation.service` с `Delegate=cpu memory pids`. Bounded trusted
anchor перемещается в свой дочерний cgroup, оставляя делегированный stage root
пустым. Скрипт печатает несекретное `CHECKER_CGROUP_HOST=/sys/fs/cgroup/.../stages`:
перенести точное значение в deployment `.env`. Runner монтирует **только** этот
subtree в заранее созданный `/run/checker-cgroup` (`NSJAIL_CGROUP_ROOT`), не создавая
mountpoint внутри Docker-owned read-only `/sys/fs/cgroup`. Сам filesystem type
проверяется по kernel mountinfo, обычный каталог с поддельными control files не
принимается. Весь host cgroup filesystem, его родитель/соседи и anchor не выдаются.

Внешний runner использует `cgroup: host`, **не** host PID/network namespace:
перемещение в отдельное делегированное дерево иначе может запрещаться containment
правилами private cgroup namespace (`nsdelegate`). Это не выдаёт writable host
cgroup mount: стандартный `/sys/fs/cgroup` замаскирован пустым read-only tmpfs,
AppArmor разрешает управление только `/run/checker-cgroup/**`. NsJail каждой стадии
создаёт новый cgroup namespace после помещения в ограниченную stage cgroup; никакой
cgroup filesystem внутрь jail не монтируется. Такая конфигурация не отменяет
необходимость реальной проверки migration/limits на целевом kernel/Engine.


Важно: stage launcher перемещает nsjail в **отдельное** делегированное дерево.
После миграции эти процессы **не учитываются** Compose memory/CPU/PID лимитами
outer runner-контейнера. Это не скрывается и не компенсируется ложным утверждением
об одном outer limit:

| Accounting boundary, N=4 | Memory | PIDs | CPU |
| --- | --- | --- | --- |
| Один stage, включая nsjail/init | 256 MiB, swap 0 | 64 | 1 CPU |
| Делегированный `stages` aggregate | 1024 MiB, swap 0 | 256 | 4 CPU |
| Его systemd parent + trusted anchor | 1088 MiB, swap 0 | 272 | 4 CPU |
| Отдельный outer runner control cgroup | 512 MiB | 128 | 1 CPU |
| Консервативный общий host upper bound parent + outer | 1600 MiB | 400 | 5 CPU |

Runtime проверяет точное соответствие `stages/memory.max`, `memory.swap.max`,
`pids.max`, `cpu.max` конфигурации N/limits, controllers и cgroup.kill. `max` либо
несогласованная делегация означают not-ready. Внешний private PID namespace остаётся
границей процессов даже после cgroup migration: смерть PID1 уничтожает его
потомков. Это необходимо испытать на конкретном Engine/kernel, а не предполагать
по одному YAML. API включает subreaper; cleanup останавливает cgroup, ждёт/reaps
процессы, подтверждает populated=0, затем читает artifact и снимает tmpfs.

Scratch `/work/runner` — отдельный bounded tmpfs 160 MiB; свежий `/work` каждого
stage — самостоятельный tmpfs до 32 MiB. Страницы, выделенные недоверенным процессом,
учитываются stage cgroup; заранее записанные API source/artifact pages и metadata —
outer control cgroup. Вложенные mounts не являются байтами parent tmpfs quota.
Search-only 0711 ancestors нужны nsjail после UID mapping; control directory 0700,
private artifacts root-owned 0500 и ни один такой каталог не монтируется в jail.
Внутри jail `/tmp` ведёт в `/work/tmp`. Private artifacts — максимум N файлов до
1 MiB; expected и каталог в runner не монтируются. Память API/bounded buffers и
watchdog входит в outer 512 MiB, swap для всех Compose memory budgets отключён.
Бюджеты БД/каталога/ОС нужно прибавить отдельно и оставить host reserve.

Compose запрашивает SYS_ADMIN для namespace/mount/pivot, SYS_CHROOT для nsjail,
SETUID/SETGID/SETPCAP для mapping/drop, CHOWN/DAC_OVERRIDE/FOWNER для закрытых tmpfs
и cleanup, KILL для supervisor. Это конкретный требуемый набор, **не заявление о
доказанной минимальности на ещё не испытанном хосте**. Внешний seccomp — настоящий
allowlist с default ERRNO, AppArmor — enforcing path/mount/capability policy.
Внутренние профили ещё строже: default KILL_PROCESS, запрет namespace flags/ptrace/
network syscalls, no capabilities, no_new_privs, UID/GID 65534. При несовместимости
профиля старт/реальный probe отказывают; privileged, unconfined, host PID namespace,
отключение seccomp/AppArmor и native gcc fallback не являются решениями.

Делегацию пересоздают после host reboot. Для остановки: сначала
`docker compose down`, затем `sudo bash deploy/provision_runner_host.sh --remove`.
Не останавливать/перенастраивать host delegation под активными jobs. При изменении
N или stage limits одновременно изменить provisioning, runner/worker env и budgets;
несогласованность обнаруживается до admission.

## Старт, readiness, восстановление и приёмка

1. Собрать/опубликовать образы, записать immutable refs, создать секреты, задать
   `PUBLIC_ORIGIN` и результат host provisioning; настроить HTTPS/firewall/backup.
2. На целевом хосте выполнить `docker compose config`, затем `docker compose up -d`.
   Это операторские команды, **не утверждение, что они уже успешно запускались**.
3. PostgreSQL health ждёт TCP final server, не временный initdb Unix socket.
   `migrate` должен закончиться успешно до web/worker/catalog. Worker зависит от
   runner health, но не от web ready: web readiness зависит от worker heartbeat,
   policy и каталога. Catalog-sync не зависит от runner.
4. Проверить локальный authenticated runner health и policy, реальный startup probe
   обоих profiles; до этого worker не должен исполнять посылки. API при проблеме
   изоляции возвращает 503; list/delete остаются аутентифицированными для recovery.
5. Запустить обычные API/unit tests и отдельно **на выделенном пустом runner**:

   ```sh
   RUNNER_SECURITY_OPT_IN=1 RUNNER_SECURITY_URL=http://... \
     RUNNER_SECURITY_TOKEN='...' uv run pytest tests/security/test_runner_isolation.py
   ```

   URL должен быть доступен проверяющему из доверенной сети этого хоста; runner порт
   не публикуется ради теста. Секрет передавать защищённым test environment, не писать
   в историю shell/логи. Эти тесты отправляют C только в runner, никогда не запускают
   gcc или student binary на хосте.
6. Дополнительно испытать отсутствие IPv6/DNS/host/API/fd/file/expected leaks,
   binary stdout, compiler warnings, fork/pipe/signals/mmap/pthread, exact byte caps,
   output flood, artifact symlink/FIFO/hardlink, cgroup OOM/PID/CPU/tmpfs и deadlines.
   Проверить populated=0 и отсутствие живых descendants/mounts после DELETE/TTL.
7. В управляемом smoke остановить worker во время run, разорвать HTTP, завершить
   API/watchdog и PID1; подтвердить independent timeout, остановку всех descendants,
   невыдачу слота до cleanup и новый recovery без blind retries. Проверить отказ при
   сломанном manifest/AppArmor/seccomp/delegation и запрет N+1 reservations.
8. Зафиксировать фактически испытанные версии/права/results и проверить backup/restore
   archive-data. Только после этого открывать приём пользовательских решений.

`RunnerClient` не повторяет compile/run при потерянном ответе. Worker сначала
перечисляет и отменяет старые handles, только затем recover/claim. Restart runner
теряет временные handles/artifacts; незавершённые jobs — infrastructure_error, не
скрытый повтор. Успешный outcome — `completed`; accepted публикует worker после
сравнения и archive receipt. Физическое уничтожение ровно к deadline не обещается
при зависшем ядре/хосте: слот карантинируется, readiness выключается.

## Источники механизмов

- [nsjail](https://github.com/google/nsjail/tree/079d70dda4aa1edd9512cfd25ff1e47e316dc355)
- [cgroup v2 delegation](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
- [Compose specification](https://docs.docker.com/reference/compose-file/)
- [Docker seccomp](https://docs.docker.com/engine/security/seccomp/)
- [Docker AppArmor](https://docs.docker.com/engine/security/apparmor/)

Ссылки и code-level tests не заменяют приведённую host security приёмку.
