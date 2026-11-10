import {api} from "./api.js";
import {createProfile} from "./profile.js";
import {createSubmission} from "./submission.js";

const profileForm = document.getElementById("profile-form");

if (profileForm) {
  const saveButton = document.getElementById("save-profile");
  const uploadButton = document.getElementById("submit-source");
  const task = document.getElementById("task");
  let csrf = null;
  let busy = false;

  function updateButtons() {
    saveButton.disabled = busy || !csrf;
    if (uploadButton) uploadButton.disabled = busy || !csrf || task.dataset.available !== "true";
  }

  function setBusy(value) {
    busy = value;
    updateButtons();
  }

  const profile = createProfile({
    getCsrf: () => csrf,
    setCsrf: value => { csrf = value; },
    isBusy: () => busy,
    setBusy
  });
  const submission = task ? createSubmission({
    task,
    profileForm,
    getCsrf: () => csrf,
    isBusy: () => busy,
    setBusy,
    saveProfile: profile.save
  }) : null;

  async function initialize() {
    try {
      const data = await api("/api/profile");
      profile.hydrate(data);
      updateButtons();
      if (submission) submission.restore(data);
    } catch (error) {
      profile.showLoadError(error);
      const resultStatus = document.getElementById("result-status");
      if (resultStatus) resultStatus.textContent = "Не удалось загрузить историю; её отсутствие не означает отсутствие посылок.";
    }
  }

  window.addEventListener("pagehide", () => {
    if (submission) submission.cancel();
  });
  initialize();
}
