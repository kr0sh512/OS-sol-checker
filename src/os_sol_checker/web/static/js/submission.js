import {api, safeText} from "./api.js";

const statuses = {
  queued: "В очереди",
  compiling: "Компиляция",
  running: "Выполнение тестов",
  archive_pending: "Все тесты пройдены. Ожидаем сохранения в архив",
  invalidated: "Результат устарел",
  finished: "Проверка завершена"
};
const verdicts = {
  accepted: "Решение принято",
  compile_error: "Ошибка компиляции",
  compile_timeout: "Таймаут компиляции",
  runtime_error: "Ошибка выполнения",
  run_timeout: "Таймаут теста",
  wrong_answer: "Неверный ответ",
  memory_limit: "Превышена память",
  output_limit: "Превышен вывод",
  submission_timeout: "Общий таймаут",
  infrastructure_error: "Ошибка инфраструктуры. Это не ошибка вашей программы"
};

export function createSubmission({task, profileForm, getCsrf, isBusy, setBusy, saveProfile}) {
  const uploadForm = document.getElementById("upload-form");
  const uploadMessage = document.getElementById("upload-message");
  const resultStatus = document.getElementById("result-status");
  let pollVersion = 0;
  let pollTimer = null;

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
        const size = document.createElement("p");
        size.textContent = `Полный размер: ${preview.byte_size} байт`;
        previews.append(size);
      }
    }
  }

  function terminal(data) {
    return data.status === "finished" || data.status === "invalidated";
  }

  function startPolling(id, firstData = null) {
    const version = ++pollVersion;
    clearTimeout(pollTimer);
    const began = Date.now();
    if (firstData) {
      render(firstData);
      if (terminal(firstData)) return;
    }
    async function poll() {
      let delay = Date.now() - began < 10000 ? 1000 : 3000;
      try {
        const data = await api("/api/submissions/" + encodeURIComponent(id));
        if (version !== pollVersion) return;
        render(data);
        if (terminal(data)) return;
      } catch (error) {
        if (version !== pollVersion) return;
        if (error.status === 404) {
          historyUnavailable();
          return;
        }
        resultStatus.textContent = "Не удалось обновить статус. Повторим через 3 секунды. " + error.message;
        delay = 3000;
      }
      if (version === pollVersion) pollTimer = setTimeout(poll, delay);
    }
    pollTimer = setTimeout(poll, firstData ? 1000 : 0);
  }

  uploadForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (isBusy() || !getCsrf() || !profileForm.reportValidity()) return;
    const file = document.getElementById("source-file").files[0];
    if (!file) {
      uploadMessage.textContent = "Выберите файл .c.";
      return;
    }
    setBusy(true);
    uploadMessage.textContent = "Сохраняем профиль…";
    try {
      await saveProfile();
      uploadMessage.textContent = "Отправляем файл…";
      const form = new FormData();
      form.append("file", file);
      const data = await api("/api/tasks/" + encodeURIComponent(task.dataset.taskId) + "/submissions", {
        method: "POST",
        headers: {"X-CSRF-Token": getCsrf()},
        body: form
      });
      uploadMessage.textContent = "Посылка принята. Новая отправка возможна только по вашему действию.";
      startPolling(data.id, data);
    } catch (error) {
      uploadMessage.textContent = error.message + " Автоматического повтора отправки не будет.";
    } finally {
      setBusy(false);
    }
  });

  function restore(profileData) {
    const last = profileData.last_submissions.find(item => item.task_id === task.dataset.taskId);
    if (!last) resultStatus.textContent = "Вы ещё не отправляли решение этого задания.";
    else if (!last.available) historyUnavailable();
    else startPolling(last.id);
  }

  function cancel() {
    pollVersion++;
    clearTimeout(pollTimer);
  }

  return {restore, cancel};
}
