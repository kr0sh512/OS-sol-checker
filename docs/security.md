# Песочница и развёртывание

## Граница доверия

Решение и компилятор, читающий решение, выполняются в gVisor (`runsc`), не в процессе API/worker и не в обычном runc-контейнере. Выбран backend Kubernetes: отдельный одноразовый Pod на компиляцию, затем отдельный одноразовый Pod на каждый тест. Между тестами не сохраняются процессы, файлы и IPC. Тесты выполняются последовательно; следующий не создаётся до остановки и удаления предыдущего Pod.

«Изоляция диска» означает отсутствие доступа к диску хоста, репозиторию тестов, БД, чужим решениям и секретам. Минимальный read-only rootfs с runtime/библиотеками и ограниченный tmpfs необходимы самой программе. Ожидаемый вывод никогда не попадает в песочницу. Нельзя обещать абсолютную безопасность: gVisor, ядро, Kubernetes и CNI — часть доверенной инфраструктуры и требуют обновлений.

API не имеет Kubernetes credentials. Worker — доверенный контроллер с namespace-scoped RBAC только на sandbox Pod и их attach/status/delete; он не получает cluster-admin, доступ к Secret, exec на чужих Pod или runtime socket. Sandbox Pod не получает service-account token. Компрометация worker остаётся серьёзным риском: admission policy обязана запрещать ослабление sandbox-спецификации, даже если worker создаёт вредоносный Pod.

## Профиль sandbox Pod

Все перечисленное обязательно для допуска в эксплуатацию, а не уже реализовано:

- Отдельный namespace и выделенный пул execution-узлов; `runtimeClassName: gvisor`, образы по digest, запуск только на узлах с проверенным runsc.
- `restartPolicy: Never`, `automountServiceAccountToken: false`; без sidecar injection и автоматически добавляемых credentials.
- `hostNetwork/hostPID/hostIPC: false`; без privileged, hostPath, PVC, устройств, сокетов Docker/containerd, Secret и пользовательских mounts.
- Непривилегированный UID/GID, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, read-only rootfs, подходящий seccomp-профиль. Не отключать seccomp ради прохождения теста.
- Только bounded memory-backed emptyDir для `/work` и `/tmp`; после Pod они уничтожаются. Скомпилированный файл и текущий stdin передаются явно, никакого общего writable volume между песочницами.
- CPU/memory/ephemeral-storage requests и limits, ограничение всего process tree по PID через конфигурацию kubelet/runtime, RLIMIT_NOFILE/FSIZE/CORE и CPU в stage launcher. `RLIMIT_NPROC` сам по себе не заменяет cgroup PID limit. `emptyDir.sizeLimit` не заменяет memory limit.
- Namespace default-deny ingress и egress, включая DNS, metadata IP, IPv4/IPv6, API кластера и соседей. CNI должен действительно обеспечивать NetworkPolicy. Loopback внутри изолированной песочницы не даёт внешней связи. Native networking passthrough в runsc не использовать.
- `activeDeadlineSeconds` не больше остатка общего бюджета, максимум 60; нулевой grace period для принудительной остановки. `backoff/retry` исполнения отсутствует: timeout/ошибка студента не перезапускают его программу.
- Admission policy проверяет RuntimeClass, securityContext, разрешённые image digests, volumes, deadline и отсутствие service-account token. ResourceQuota дополнительно ограничивает число Pod и ресурсы namespace.

Без работающего RuntimeClass, CNI enforcement, PID limit или admission-политики worker не принимает задания; readiness false. Никакого fallback на локальный subprocess или runc.

## Передача данных и запуск

`SandboxBackend` — единственный владелец transport-протокола. Stage launcher и образы живут в `sandbox/` и `deploy/containers/`; контроллер — в `adapters/kubernetes/`.

1. Worker создаёт Pod с фиксированными image/argv, `stdin: true`, `stdinOnce: true`, `tty: false`. Основной процесс ждёт ограниченный запрос на stdin. Передача через Kubernetes attach не требует открытого порта в sandbox network. Установка attach/доставка имеют deadline; потерянная сессия не оставляет бессрочно ждущий Pod.
2. Запрос — версионированный length-prefixed header с ограничением 4 КиБ и строго заданные поля длины payload. Compile payload — исходные байты; run payload — скомпилированные байты и stdin ровно одного теста. Header не задаёт произвольные команды, флаги, mounts, env или пути.
3. Compile launcher пишет `/work/main.c`, вызывает GCC фиксированным массивом argv без shell, ограничивает diagnostics и размер артефакта. Compiler stdout/stderr не смешиваются с каналом передачи бинарного файла. Ответ компиляции содержит bounded diagnostics и length-prefixed artifact; всё полученное worker считает недоверенными байтами. Exit code подтверждается состоянием контейнера, а не текстом ответа.
4. Run launcher полностью читает и проверяет payload до запуска, создаёт executable и stdin в tmpfs, закрывает control stream и лишние fd, выставляет rlimits, фиксированную среду и cwd. Затем делает exec бинарного файла, перенаправив fd 0 на текущий stdin. Никаких следующих тестов, expected output или секретов в файлах, аргументах, среде либо открытых fd нет.
5. Worker читает stdout/stderr раздельно, с ограничением в ходе чтения, без TTY, без shell и без преобразования байтов до сравнения. Exit code/signal берётся из наблюдения контейнера. Текст программы не может объявить `accepted`, подменить таймаут или имитировать служебный ответ.
6. На terminal outcome, превышении лимита, исключении или обрыве связи worker закрывает transport, останавливает и удаляет Pod в `finally`. Выход основного процесса должен прекращать также потомков; fork/setsid не продлевают жизнь sandbox. Отдельно подтвердить отсутствие оставшегося sandbox на runtime-уровне.

Входные наборы и expected output читает только worker из immutable catalog snapshot. Они никогда не монтируются в sandbox целиком. Кешируются только результаты, не изменяемые исполняемые артефакты разных посылок.

## Компилятор и сравнение

Начальный argv (имя файла на сервере фиксировано):

```text
gcc -std=gnu17 -O2 -pipe -Wall -Wextra -Wformat=2 -Werror=format-security
    -fstack-protector-strong -D_FORTIFY_SOURCE=2 -fPIE -pie
    -Wl,-z,relro,-z,now /work/main.c -o /work/main -lm -pthread
```

Это единый список аргументов, не shell-строка с переносами. Общего `-Werror` нет: обычные предупреждения не делают корректное решение неуспешным. Не принимать пользовательские опции GCC. Не выполнять сборку на этапе web-запроса. Образ компилятора содержит совместимый runtime с образом исполнения; версии GCC/libc и flags digest включаются в контекст проверки.

Начальный comparator — точное сравнение stdout с `.out`, включая пробелы и завершающий LF. Stderr не сравнивается с expected stdout, но показывается как ограниченная диагностика. Nonzero exit/signal — runtime_error даже при совпавшем stdout. Сначала проверяется завершение/ресурсная причина, затем совпадение вывода. Остановка после первого непрошедшего теста обязательна.

gVisor не реализует весь Linux без исключений. Перед запуском курса проверить реальные задачи и отдельно fork/wait, pipe, signals, mmap и pthread. Задачи, требующие внешней сети, доступа к хосту или неподдерживаемых syscall, несовместимы с выбранной политикой; не ослаблять изоляцию автоматически.

## Очередь, слоты и дедлайны

- Одна временная PostgreSQL-очередь для всех реплик API, FIFO по принятому порядку. Число queued ограничено конфигурацией; при заполнении отказ 503 до создания принятой посылки. Повторная отправка не должна обходить лимит размера или доступа.
- Один активный scheduler-worker, защищённый PostgreSQL advisory lock на отдельном соединении; Kubernetes Deployment `replicas: 1`, strategy Recreate. Увеличение replicas не является способом масштабирования sandbox throughput. Параллелизм задаётся `MAX_CONCURRENT_SANDBOXES=N`.
- Worker резервирует один из N слотов на всю посылку. В слоте одновременно не больше одного Pod. Компиляция и тесты используют тот же слот; освобождение — только после подтверждённой остановки и удаления ресурсов. Поэтому глобально sandbox не больше N.
- При потере advisory-lock соединения worker немедленно перестаёт создавать Pod и завершает доступные ему stage. Новый worker сначала находит все старые Pod по labels submission/generation/slot, останавливает и убирает их; только потом открывает очередь. Admission/ResourceQuota страхуют создание лишних Pod при гонках и сбоях.
- 10 секунд на компиляцию, 2 секунды на каждый запуск — monotonic wall time активной стадии; передача запроса не может сделать время неограниченным. 60 секунд — весь активный жизненный цикл посылки с момента claim, включая запуск Pod и cleanup; ожидание очереди не расходует его. Deadline не сбрасывается между тестами.
- Время до начала процесса (например, image pull/scheduling) ограничено общим deadline. Если процесс не запущен по вине инфраструктуры — infrastructure_error, не compile_timeout/run_timeout. Исчерпание общего бюджета во время проверки — submission_timeout. Приоритет причин задаётся явными событиями контроллера, не произвольным exit 137.
- Worker watchdog и Pod activeDeadlineSeconds — два слоя остановки. При недоступном/зависшем узле Kubernetes не гарантирует физическое завершение ровно за 60 секунд. Для строгого wall-clock предела нужен независимый node-local watchdog, убивающий runtime sandbox по исходному deadline даже при потере API/worker. Это обязательный deployment prerequisite для заявленного жёсткого лимита; без него нельзя объявлять требование выполненным. Новый слот не выдаётся, пока остановка неизвестна.
- После первого сбоя теста остальные Pod не создаются. После получения ответа sandbox завершается немедленно, а не ждёт конца deadline. При смене generation активная работа отменяется, старый результат не публикуется как актуальный.

## Хранение и эксплуатация

- Web/worker работают non-root. PostgreSQL credentials — через Secret, разные роли для временной БД и архива. Web не читает постоянные исходники; успешные исходники сохраняет worker. В логи не писать код, stdin, cookie, имена/группы и полный stderr. Для корреляции достаточно submission_id, task_id, generation и verdict.
- Временная БД размещается на ephemeral storage без backup; перезапуск может потерять очередь, результаты и сессии — интерфейс сообщает об этом честно. Контроллер при старте всё равно убирает оставшиеся Pod. Архив — отдельный persistent volume/управляемая БД с backup и проверкой восстановления.
- Каталог доступен web/worker как read-only snapshot store; только sync-процесс пишет новые снимки. Git-синхронизация имеет доступ к upstream, sandbox — нет. Web не принимает URL произвольного репозитория от пользователя.
- HTTPS ingress, ограничение upload body и частоты приёма/опроса; лимит файла дополнительно внутри API. Session ownership и CSRF обязательны. Временный профиль не является подтверждением личности студента.
- Markdown рендерится с отключённым raw HTML и проходит allowlist-санитизацию. Ссылки допускают только безопасные http/https или same-origin относительные URL; запрещены javascript/data/file и protocol-relative URL. Внешние изображения и HTML-вставки не загружаются; изображения из Git не публикуются автоматически. Скрипты приложения только same-origin, без inline-скриптов; CSP запрещает object/frame и посторонние script origins. Обычные ссылки на внешние сайты не передают cookie сессии.
- `deploy/containers/`: application, compiler, runner Dockerfile. `deploy/kubernetes/`: workloads, sandbox template, network policies, RBAC, admission policy, quotas, storage/config. Node provisioning runsc/CNI/watchdog описывается в итоговой инструкции развёртывания, не прячется внутрь application image.
- В этой заготовке Dockerfile и YAML намеренно состоят из комментариев: их нельзя считать собранными образами или готовым deployment. Реализующий агент должен предоставить настоящие manifests и испытать их на целевом runtime, а не заменить gVisor Docker Compose-эмуляцией.

## Первичные источники

- [gVisor security model](https://gvisor.dev/docs/architecture_guide/security/).
- [containerd/runsc и Kubernetes RuntimeClass](https://gvisor.dev/docs/user_guide/containerd/quick_start/).
- [Kubernetes NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/).
- [Kubernetes Pod lifecycle и activeDeadlineSeconds](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/).

Ссылки объясняют выбранные механизмы; не являются доказательством безопасности ещё не написанного приложения.
