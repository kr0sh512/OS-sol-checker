import {api} from "./api.js";

export function createProfile({getCsrf, setCsrf, isBusy, setBusy}) {
  const form = document.getElementById("profile-form");
  const name = document.getElementById("name");
  const group = document.getElementById("group");
  const message = document.getElementById("profile-message");
  const initial = {name: name.value, group: group.value};

  async function save() {
    const data = await api("/api/profile", {
      method: "PUT",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": getCsrf()},
      body: JSON.stringify({name: name.value, group: group.value})
    });
    setCsrf(data.csrf_token);
    message.textContent = "Профиль сохранён. Авторы ранее отправленных решений не изменились.";
    return data;
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (isBusy() || !getCsrf() || !form.reportValidity()) return;
    setBusy(true);
    try {
      await save();
    } catch (error) {
      message.textContent = error.message;
    } finally {
      setBusy(false);
    }
  });

  function hydrate(data) {
    setCsrf(data.csrf_token);
    if (name.value === initial.name) name.value = data.profile.name || "";
    if (group.value === initial.group) group.value = data.profile.group || "";
    message.textContent = data.profile.name && data.profile.group
      ? "Профиль загружен."
      : "Укажите имя и группу перед отправкой.";
  }

  function showLoadError(error) {
    message.textContent = "Не удалось загрузить профиль. Перезагрузите страницу. " + error.message;
  }

  return {form, save, hydrate, showLoadError};
}
