const form = document.querySelector("#login-form");
const username = document.querySelector("#username");
const password = document.querySelector("#password");
const submitButton = document.querySelector("#login-button");
const errorBox = document.querySelector("#login-error");
const showPassword = document.querySelector("#show-password");

showPassword.addEventListener("click", () => {
  const visible = password.type === "text";
  password.type = visible ? "password" : "text";
  showPassword.setAttribute("aria-label", visible ? "Show password" : "Hide password");
  showPassword.title = visible ? "Show password" : "Hide password";
  password.focus();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.textContent = "";
  submitButton.disabled = true;
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.value, password: password.value }),
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* The error below is sufficient. */ }
    if (!response.ok) throw new Error(payload.error || `Sign in failed (${response.status})`);
    password.value = "";
    window.location.replace("/camera");
  } catch (error) {
    errorBox.textContent = error.message;
    password.select();
  } finally {
    submitButton.disabled = false;
  }
});
