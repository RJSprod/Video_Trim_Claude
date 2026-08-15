/* The login form.
 *
 * Posts JSON so the response can carry a readable message, but the form still
 * has a real action and method, so it works if this script never loads.
 */
(function () {
  "use strict";

  var form = document.getElementById("vt-login-form");
  var status = document.getElementById("vt-login-status");
  if (!form) return;

  function say(message, tone) {
    if (!status) return;
    status.textContent = message || "";
    if (tone) {
      status.setAttribute("data-tone", tone);
    } else {
      status.removeAttribute("data-tone");
    }
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var button = form.querySelector("button[type=submit]");
    var username = (document.getElementById("vt-username") || {}).value || "";
    var password = (document.getElementById("vt-password") || {}).value || "";

    if (button) button.disabled = true;
    say("Signing in…");

    fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ username: username, password: password })
    })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          if (!response.ok) {
            throw new Error(body.detail || "Sign in failed.");
          }
          return body;
        });
      })
      .then(function (body) {
        say("Signed in.");
        window.location.href = body.redirect || "/";
      })
      .catch(function (error) {
        if (button) button.disabled = false;
        // Deliberately whatever the server said, which never reveals whether
        // the username exists.
        say(error.message || "Sign in failed.", "deny");
        var field = document.getElementById("vt-password");
        if (field) { field.value = ""; field.focus(); }
      });
  });

  var first = document.getElementById("vt-username");
  if (first && !first.value) first.focus();
})();
