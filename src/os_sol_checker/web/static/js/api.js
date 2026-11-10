export async function api(url, options = {}) {
  const response = await fetch(url, {credentials: "same-origin", cache: "no-store", ...options});
  let data;
  try {
    data = await response.json();
  } catch (_) {
    throw Object.assign(new Error("Сервер вернул непонятный ответ. Попробуйте позднее."), {status: response.status});
  }
  if (!response.ok) {
    throw Object.assign(new Error(data.error?.message || "Запрос не выполнен."), {status: response.status});
  }
  return data;
}

export function safeText(value, bound) {
  return String(value ?? "").slice(0, bound).replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g,
    character => "\\u" + character.charCodeAt(0).toString(16).padStart(4, "0"));
}
