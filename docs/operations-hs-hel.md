# Реальное развёртывание hs-hel

Проверено 2026-11-08: https://os.krosh.dev обслуживает настоящий Compose web,
не maintenance backend. Успешная отправка действительно компилировалась и
исполнялась в nsjail, прошла 2/2 теста и получила подтверждение постоянного архива.
Повтор получил другой UUID и cache hit. Невалидный C дал compile_error.

## Размещение и ограничения

Хост `hs-hel`, SSH `krosh@100.100.10.1:2006`, `/opt/os-sol-checker`.
Rootful Docker 29.9.2, Compose v5.6.1, Linux 7.0.0-38-generic, cgroup v2,
enforcing AppArmor и seccomp. Основная топология — оригинальный Compose,
приложение не переносилось в Kubernetes. Host overlay, systemd units, TLS и
Ingress находятся в отдельном источнике конфигурации:
`~/Docs/k8s/apps/os-sol-checker/` и
`~/Docs/k8s/infrastructure/ingress/os-sol-checker/`.

**Это общий сервер с control-plane K3s и другими сервисами, не выделенный хост
без посторонних секретов.** Пройденные проверки не устраняют общий kernel/blast
radius. Для более сильной границы требуется отдельная VM/машина. Это ограничение
не скрывается под формулировкой «песочница доказанно безопасна».

## Исправления, выявленные настоящим запуском

- Закреплённый kafel использует kernel syscall names: `newstat`, `newfstat`,
  `newlstat`, `newuname`, `sendfile64`, аргумент `clone_flags`. Более новые
  clone3/rseq/faccessat2 заданы точными x86-64 ABI номерами. Seccomp default остаётся
  KILL_PROCESS, namespace/socket/ptrace authority не добавлялась.
- На стадии сборки `sandbox/check_policies.c` действительно компилирует оба
  профиля библиотекой **того же** kafel. Некорректный профиль ломает сборку.
- Builder rootfs вычисляет относительные library symlinks относительно реального
  parent: `/lib` в jail ведёт в `/usr/lib`. Добавлены проверка broken symlinks и
  регрессия без компиляции или выполнения C на хосте.
- Docker masked submounts в outer `/proc` запрещали mount нового procfs во вложенной
  user namespace. Доверенный startup монтирует чистый procfs **в приватной outer PID
  namespace**, а не host procfs; AppArmor разрешает запись только в uid/gid maps.
  Дочерний procfs остаётся новым/read-only; namespace/fd/capability/security probes
  пройдены. Docker systempaths, AppArmor и seccomp не отключались.
- AppArmor дополнен точечными directory/mount permissions для nsjail staging,
  чтением публичного CA bundle и приёмом сигналов от confined `runc` Docker 29.
  Нет unconfined/privileged/host PID/Docker socket обходов.
- Web получил только `SELECT (heartbeat_at)` на worker_status для readiness,
  не запись scheduler heartbeat. Real-PostgreSQL role regression проверяет это.
- RunnerClient использует общий domain RunnerError, который реально перехватывает
  scheduler, вместо отдельного несовместимого exception type.
- Git ingestion сохраняет только operator HTTPS proxy variables в своём очищенном
  окружении. Секреты других ролей в дочерний Git не передаются. Это позволило
  закрыть прямой catalog Internet и использовать allowlisted CONNECT proxy.

## Наблюдаемые проверки

- Регрессии с двумя настоящими отдельными PostgreSQL test DB внутри закрытого
  test container: **206 passed, 11 skipped** за 7.91 с. Пропуски — opt-in runner
  suite. Одно стороннее предупреждение Starlette/httpx TestClient сохранено.
- Отдельно, финальный runner image и приватная worker-runner сеть:
  `RUNNER_SECURITY_OPT_IN=1 ... pytest tests/security/test_runner_isolation.py`:
  **11 passed** за 4.42 с. Scheduler на время suite остановлен.
- Проверены binary stdio/fork/wait/pipe/threads, отсутствие control secrets/лишних
  fd, запрет socket IPv4/IPv6, output/memory/PID/artifact/tmpfs limits, read-only
  root и отказ N+1 reservation.
- При убийстве API, watchdog и PID1 во время fork/setsid run все descendants
  исчезли, stages cgroups опустели, runner восстановился. Ни один такой тест не
  выполнял решение вне nsjail.
- С заведомо неправильным cgroup root: health=503, reservation=503,
  executions=[], execution fallback отсутствует. Испытание выполнялось
  последовательно, основной runner/worker остановлены и затем восстановлены.
- После потери transient container systemd startup заново создал БД, применил
  миграции, опубликовал каталог/политику и восстановил readiness=200.
  Полная перезагрузка общего сервера не выполнялась.
- Через публичный Chromium: профиль/навигация/upload/accepted+archive/cache с новым
  UUID/compile_error/reload. На 390 CSS px scrollWidth=390. HTTPS без `-k`,
  HTTP redirect, request 131073 bytes отклонён с 413.
- Catalog напрямую не достигает Internet; CONNECT к произвольному домену и
  metadata IP запрещены, разрешённый GitHub import работает.
- После cleanup `stages/cgroup.events` содержит populated=0, stage directories нет.
- Custom dump архива восстановлен в отдельную БД, counts blobs/solutions/receipts
  совпали (1/1/2 на момент проверки). Test DB удалена. Проверенная SHA-256 копия
  вынесена на рабочую машину вне сервера.

## Эксплуатация

Application/runner images опубликованы в локальный loopback-only registry и
закреплены настоящими RepoDigests в серверном `.env`. Пароли находятся только в
role-scoped `deploy/secrets`, не в образах или manifests.

`os-sol-checker.service` сначала останавливает edge/application/runner, поднимает
БД, применяет owner migrations, проверяет runner/web readiness и лишь затем
включает edge. Unit зависит от `checker-runner-host.service`, восстанавливающего
ограниченную cgroup delegation. Не менять delegation под работающим runner.

Traefik завершает TLS; путь до hs-hel защищён Tailscale. Edge слушает только
100.100.10.1:18000 и ограничивает source ingress node/PodCIDR, далее обращается к
неизменённому 127.0.0.1:8000. Runner/БД не опубликованы.

Ежедневный backup timer пишет dump архива и SHA-256 в root-only
`/var/backups/os-sol-checker`. Автоматическая регулярная off-host доставка пока
не настроена: вне сервера есть проверенный первоначальный dump. Для disaster
recovery отдельно сохранять защищённые bootstrap secrets и конфигурацию.

Upstream на момент запуска содержит **одну примерную задачу и две пустые пары
тестов**. Accepted подтверждает прохождение pipeline, а не полноту учебной проверки.
Нужны реальные учебные задания/тесты и дальнейшее обновление kernel/runtime/images.
