"use strict";
(() => {
  const profileForm = document.getElementById("profile-form");
  if (!profileForm) return;
  const name = document.getElementById("name");
  const group = document.getElementById("group");
  const initial = {name: name.value, group: group.value};
  const profileMessage = document.getElementById("profile-message");
  const saveButton = document.getElementById("save-profile");
  const uploadForm = document.getElementById("upload-form");
  const uploadButton = document.getElementById("submit-source");
  const task = document.getElementById("task");
  const uploadMessage = document.getElementById("upload-message");
  let csrf = null;
  let busy = false;
  let pollVersion = 0;
  let pollTimer = null;
  const statuses = {queued: "В очереди", compiling: "Компиляция", running: "Выполнение тестов",
    archive_pending: "Все тесты пройдены. Ожидаем сохранения в архив", invalidated: "Результат устарел",
    finished: "Проверка завершена"};
  const verdicts = {accepted: "Решение принято", compile_error: "Ошибка компиляции",
    compile_timeout: "Таймаут компиляции", runtime_error: "Ошибка выполнения", run_timeout: "Таймаут теста",
    wrong_answer: "Неверный ответ", memory_limit: "Превышена память", output_limit: "Превышен вывод",
    submission_timeout: "Общий таймаут", infrastructure_error: "Ошибка инфраструктуры. Это не ошибка вашей программы"};
  const resultStatus = document.getElementById("result-status");

  function buttons() {
    saveButton.disabled = busy || !csrf;
    if (uploadButton) uploadButton.disabled = busy || !csrf || task.dataset.available !== "true";
  }
  async function api(url, options = {}) {
    const response = await fetch(url, {credentials: "same-origin", cache: "no-store", ...options});
    let data;
    try { data = await response.json(); }
    catch (_) { throw Object.assign(new Error("Сервер вернул непонятный ответ. Попробуйте позднее."), {status: response.status}); }
    if (!response.ok) throw Object.assign(new Error(data.error?.message || "Запрос не выполнен."), {status: response.status});
    return data;
  }
  function safeText(value, bound) {
    return String(value ?? "").slice(0, bound).replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g,
      c => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
  }
  async function saveProfile() {
    const data = await api("/api/profile", {method: "PUT", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
      body: JSON.stringify({name: name.value, group: group.value})});
    csrf = data.csrf_token;
    profileMessage.textContent = "Профиль сохранён. Авторы ранее отправленных решений не изменились.";
    return data;
  }
  profileForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !csrf || !profileForm.reportValidity()) return;
    busy = true; buttons();
    try { await saveProfile(); }
    catch (error) { profileMessage.textContent = error.message; }
    finally { busy = false; buttons(); }
  });

  function historyUnavailable() {
    resultStatus.textContent = "История недоступна. Вы можете явно отправить новое решение.";
    document.getElementById("result-content").hidden = true;
  }
  function render(data) {
    document.getElementById("result-content").hidden = false;
    document.getElementById("submission-id").value = data.id;
    let label = verdicts[data.verdict] || statuses[data.status] || "Проверка";
    if (data.status === "queued" && data.queue_position != null) label += ": позиция " + data.queue_position;
    resultStatus.textContent = label;
    document.getElementById("result-cache").textContent = data.cache_hit ? "Результат из кэша" : "";
    document.getElementById("result-counts").textContent = `Пройдено тестов: ${data.passed_tests} из ${data.total_tests}`;
    document.getElementById("result-context").textContent = `Поколение ${data.generation}. Принято: ${new Date(data.created_at).toLocaleString("ru-RU")}. Политика: ${safeText(data.context.judge_policy_digest, 12)}…`;
    document.getElementById("result-stale").hidden = !data.stale;
    const failure = data.first_failure;
    document.getElementById("first-failure").hidden = !failure;
    const previews = document.getElementById("failure-previews");
    previews.replaceChildren();
    if (!failure) return;
    document.getElementById("failure-message").textContent = safeText(failure.message, 4096);
    for (const [field, label] of [["diagnostics", "Диагностика"], ["stdin_preview", "Вход"],
      ["expected_preview", "Ожидаемый вывод"], ["actual_preview", "Полученный вывод"]]) {
      const preview = failure[field];
      if (!preview) continue;
      const heading = document.createElement("h4");
      heading.textContent = label + (preview.truncated ? " (показан фрагмент)" : "");
      const pre = document.createElement("pre");
      pre.textContent = safeText(preview.text, field === "diagnostics" ? 49152 : 24576);
      previews.append(heading, pre);
      if (preview.byte_size != null) {
        const size = document.createElement("p"); size.textContent = `Полный размер: ${preview.byte_size} байт`;
        previews.append(size);
      }
    }
  }
  function terminal(data) { return data.status === "finished" || data.status === "invalidated"; }
  function startPolling(id, firstData = null) {
    const version = ++pollVersion;
    clearTimeout(pollTimer);
    const began = Date.now();
    if (firstData) { render(firstData); if (terminal(firstData)) return; }
    async function poll() {
      let delay = Date.now() - began < 10000 ? 1000 : 3000;
      try {
        const data = await api("/api/submissions/" + encodeURIComponent(id));
        if (version !== pollVersion) return;
        render(data);
        if (terminal(data)) return;
      } catch (error) {
        if (version !== pollVersion) return;
        if (error.status === 404) { historyUnavailable(); return; }
        resultStatus.textContent = "Не удалось обновить статус. Повторим через 3 секунды. " + error.message;
        delay = 3000;
      }
      if (version === pollVersion) pollTimer = setTimeout(poll, delay);
    }
    pollTimer = setTimeout(poll, firstData ? 1000 : 0);
  }
  if (uploadForm) uploadForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !csrf || !profileForm.reportValidity()) return;
    const file = document.getElementById("source-file").files[0];
    if (!file) { uploadMessage.textContent = "Выберите файл .c."; return; }
    busy = true; buttons(); uploadMessage.textContent = "Сохраняем профиль…";
    try {
      await saveProfile();
      uploadMessage.textContent = "Отправляем файл…";
      const form = new FormData(); form.append("file", file);
      const data = await api("/api/tasks/" + encodeURIComponent(task.dataset.taskId) + "/submissions", {
        method: "POST", headers: {"X-CSRF-Token": csrf}, body: form});
      uploadMessage.textContent = "Посылка принята. Новая отправка возможна только по вашему действию.";
      startPolling(data.id, data);
    } catch (error) { uploadMessage.textContent = error.message + " Автоматического повтора отправки не будет."; }
    finally { busy = false; buttons(); }
  });
  async function initialize() {
    try {
      const data = await api("/api/profile");
      csrf = data.csrf_token;
      if (name.value === initial.name) name.value = data.profile.name || "";
      if (group.value === initial.group) group.value = data.profile.group || "";
      profileMessage.textContent = data.profile.name && data.profile.group ? "Профиль загружен." : "Укажите имя и группу перед отправкой.";
      buttons();
      if (task) {
        const last = data.last_submissions.find(item => item.task_id === task.dataset.taskId);
        if (!last) resultStatus.textContent = "Вы ещё не отправляли решение этого задания.";
        else if (!last.available) historyUnavailable();
        else startPolling(last.id);
      }
    } catch (error) {
      profileMessage.textContent = "Не удалось загрузить профиль. Перезагрузите страницу. " + error.message;
      if (resultStatus) resultStatus.textContent = "Не удалось загрузить историю; её отсутствие не означает отсутствие посылок.";
    }
  }
  window.addEventListener("pagehide", () => { pollVersion++; clearTimeout(pollTimer); });
  initialize();
})();
