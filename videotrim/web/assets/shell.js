/* The application shell: Home cards, Media Transfer, Settings.
 *
 * One script for all three views, dispatched on body[data-view], because they
 * share the same request helper, the same status vocabulary and the same rule
 * about never inventing a destination string the server did not send.
 *
 * Nothing here decides what is allowed. Cards are drawn from server-reported
 * capabilities and every action is refused server-side on its own merits, so a
 * hidden control is a courtesy rather than a control.
 */
(function () {
  "use strict";

  var view = document.body.getAttribute("data-view") || "";

  // --- icons ----------------------------------------------------------------
  var ICONS = {
    film: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="4" width="19" height="16" rx="3"/><path d="M7 4v16M17 4v16M2.5 12h19M2.5 8h4.5M2.5 16h4.5M17 8h4.5M17 16h4.5"/></svg>',
    send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M6 11l6-6 6 6"/><path d="M4 20h16"/></svg>',
    sliders: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2.2"/><circle cx="10" cy="17" r="2.2"/></svg>',
    spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5l1.9 4.9 4.9 1.9-4.9 1.9L12 17.1l-1.9-4.9-4.9-1.9 4.9-1.9z"/><path d="M18.5 16.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"/></svg>',
    folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.5h7A1.5 1.5 0 0 1 19 10v7.5A1.5 1.5 0 0 1 17.5 19h-13A1.5 1.5 0 0 1 3 17.5z"/></svg>',
    grid: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7.5" height="7.5" rx="1.8"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.8"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.8"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.8"/></svg>',
    up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V6"/><path d="M6 12l6-6 6 6"/></svg>'
  };

  // --- requests -------------------------------------------------------------
  function cookie(name) {
    var parts = (document.cookie || "").split(";");
    for (var i = 0; i < parts.length; i++) {
      var pair = parts[i].trim();
      var eq = pair.indexOf("=");
      if (eq > 0 && pair.slice(0, eq) === name) return pair.slice(eq + 1);
    }
    return "";
  }

  /* Every state change carries the session's CSRF token in a custom header.
   * A cross-site form cannot set one, and the value is checked against the
   * session record server-side rather than merely echoed. */
  function vtFetch(url, options) {
    options = options || {};
    options.credentials = "same-origin";
    options.headers = options.headers || {};
    var method = (options.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      options.headers["X-VT-CSRF"] = cookie("vt_csrf");
    }
    return fetch(url, options).then(function (response) {
      if (response.status === 401) {
        window.location.href = "/login";
        throw new Error("Login required");
      }
      var isJson = (response.headers.get("content-type") || "").indexOf("json") >= 0;
      return (isJson ? response.json() : response.text()).then(function (body) {
        if (!response.ok) {
          var detail = body && body.detail ? body.detail : "Request failed (" + response.status + ")";
          throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        }
        return body;
      });
    });
  }

  function postJson(url, payload) {
    return vtFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    });
  }

  function byId(id) { return document.getElementById(id); }

  function say(node, message, tone) {
    if (!node) return;
    node.textContent = message || "";
    if (tone) node.setAttribute("data-tone", tone);
    else node.removeAttribute("data-tone");
  }

  function fmtTime(seconds) {
    if (!seconds) return "never";
    try { return new Date(seconds * 1000).toLocaleString(); }
    catch (err) { return "unknown"; }
  }

  // --- sign out -------------------------------------------------------------
  function wireSignOut() {
    var button = byId("vt-signout");
    if (!button) return;
    button.addEventListener("click", function () {
      postJson("/api/logout")
        .then(function (body) { window.location.href = body.redirect || "/login"; })
        .catch(function () { window.location.href = "/login"; });
    });
  }

  // --- Home -----------------------------------------------------------------
  function renderHome() {
    var list = byId("vt-cards");
    if (!list) return;

    vtFetch("/vt/api/capabilities").then(function (caps) {
      list.innerHTML = "";
      (caps.tools || []).forEach(function (tool) {
        var item = document.createElement("li");
        // A tool with no capability is not rendered as a dead card the user has
        // to discover is dead — it simply is not offered.
        if (!tool.enabled && tool.id === "settings") return;

        var node = document.createElement(tool.enabled && tool.route ? "a" : "div");
        node.className = "vt-glass vt-card";
        if (tool.enabled && tool.route) {
          node.href = tool.route;
        } else {
          node.setAttribute("aria-disabled", "true");
          node.setAttribute("role", "group");
        }

        var icon = document.createElement("span");
        icon.className = "vt-card-icon";
        icon.setAttribute("aria-hidden", "true");
        icon.innerHTML = ICONS[tool.icon] || ICONS.spark;

        var body = document.createElement("span");
        body.className = "vt-card-body";

        var title = document.createElement("span");
        title.className = "vt-card-title";
        title.textContent = tool.title;

        var text = document.createElement("span");
        text.className = "vt-card-text";
        text.textContent = tool.description;

        body.appendChild(title);
        body.appendChild(text);

        if (!tool.enabled) {
          var tag = document.createElement("span");
          tag.className = "vt-card-tag";
          tag.textContent = "Coming soon";
          body.appendChild(tag);
          // Announced as unavailable rather than merely looking faded.
          node.setAttribute("aria-label", tool.title + " — coming soon, not available yet");
        }

        node.appendChild(icon);
        node.appendChild(body);
        item.appendChild(node);
        list.appendChild(item);
      });
    }).catch(function (error) {
      list.innerHTML = "";
      var item = document.createElement("li");
      item.className = "vt-empty";
      item.textContent = error.message;
      list.appendChild(item);
    });
  }

  // --- Media Transfer -------------------------------------------------------
  function renderTransfer() {
    var destination = byId("vt-transfer-destination");
    var denied = byId("vt-transfer-denied");
    var deniedText = byId("vt-denied-text");
    var picker = byId("vt-transfer-picker");
    var pick = byId("vt-transfer-pick");
    var input = byId("vt-transfer-input");
    var list = byId("vt-transfer-list");
    var requestButton = byId("vt-request-access");
    var requestStatus = byId("vt-request-status");

    function refresh() {
      return vtFetch("/vt/api/transfer/status").then(function (status) {
        // Whatever the server called the destination is what is shown. The
        // string is a path only when the server decided this session may see one.
        if (!status.output_configured) {
          destination.textContent = "Save location is unavailable — the host has not chosen one yet.";
        } else {
          destination.textContent = "Files are copied to " + status.destination + ".";
        }

        var allowed = status.allowed && status.output_configured;
        picker.hidden = !allowed;
        denied.hidden = status.allowed;

        if (!status.allowed) {
          deniedText.textContent = status.reason || "File transfer disabled by host";
          requestButton.hidden = !status.can_request_access;
          if (status.access_requested) {
            say(requestStatus, "Access requested — the host will see this on their machine");
          }
        }
        return status;
      }).catch(function (error) {
        destination.textContent = error.message;
      });
    }

    if (requestButton) {
      requestButton.addEventListener("click", function () {
        requestButton.disabled = true;
        postJson("/vt/api/transfer/request-access")
          .then(function (body) {
            say(requestStatus, body.message ||
              "Access requested — the host will see this on their machine");
          })
          .catch(function (error) { say(requestStatus, error.message, "deny"); })
          .then(function () { requestButton.disabled = false; });
      });
    }

    if (pick && input) {
      pick.addEventListener("click", function () { input.click(); });
      input.addEventListener("change", function () {
        var files = Array.prototype.slice.call(input.files || []);
        input.value = "";
        files.forEach(sendOne);
      });
    }

    /* Two phases, reported separately. "Uploaded" is not "saved": the bytes have
     * reached the host's staging area, and the copy into the save folder has
     * not happened yet. Collapsing them would let a failed commit look like a
     * success. */
    function sendOne(file) {
      var row = document.createElement("li");
      row.className = "vt-transfer-item";
      row.setAttribute("data-status", "uploading");
      row.innerHTML =
        '<div class="vt-transfer-head">' +
        '<span class="vt-transfer-name"></span>' +
        '<span class="vt-transfer-state">Sending…  0%</span>' +
        "</div>" +
        '<div class="vt-bar"><div class="vt-bar-fill"></div></div>' +
        '<p class="vt-transfer-note"></p>';
      row.querySelector(".vt-transfer-name").textContent = file.name;
      list.insertBefore(row, list.firstChild);

      var state = row.querySelector(".vt-transfer-state");
      var fill = row.querySelector(".vt-bar-fill");
      var note = row.querySelector(".vt-transfer-note");

      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/vt/api/transfer/upload?name=" + encodeURIComponent(file.name));
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("X-VT-CSRF", cookie("vt_csrf"));
      xhr.withCredentials = true;

      xhr.upload.onprogress = function (event) {
        if (!event.lengthComputable) return;
        var pct = Math.round((event.loaded / event.total) * 100);
        fill.style.width = pct + "%";
        state.textContent = "Sending…  " + pct + "%";
      };
      xhr.upload.onload = function () {
        fill.style.width = "100%";
        state.textContent = "Finalizing…";
      };
      xhr.onload = function () {
        var body = {};
        try { body = JSON.parse(xhr.responseText || "{}"); } catch (err) { /* ignore */ }

        if (xhr.status === 401) { window.location.href = "/login"; return; }

        if (xhr.status >= 200 && xhr.status < 300) {
          var status = body.status || "saved";
          row.setAttribute("data-status", status);
          if (status === "saved") {
            state.textContent = "Saved";
            note.textContent = body.saved_name || "";
          } else if (status === "already_exists") {
            // A skip is not a failure, and is not styled as one.
            state.textContent = "Already exists — skipped";
            note.textContent = "Nothing was replaced.";
          } else {
            state.textContent = "Check this one";
            note.textContent = body.message || "";
          }
          return;
        }

        row.setAttribute("data-status", xhr.status === 403 ? "blocked" : "failed");
        state.textContent = xhr.status === 403 ? "Blocked" : "Failed";
        note.textContent = body.detail || ("Transfer failed (" + xhr.status + ")");
        if (xhr.status === 403) refresh();
      };
      xhr.onerror = function () {
        row.setAttribute("data-status", "failed");
        state.textContent = "Failed";
        note.textContent = "The connection dropped.";
      };
      xhr.send(file);
    }

    refresh();
  }

  // --- Settings -------------------------------------------------------------
  function renderSettings() {
    var saveStatus = byId("vt-save-status");
    var accountStatus = byId("vt-account-status");

    function load() {
      return vtFetch("/vt/api/settings").then(function (data) {
        byId("vt-account-user").textContent = data.username || "—";
        var accountName = byId("vt-account-username");
        if (accountName && !accountName.value) accountName.value = data.username || "";

        var current = byId("vt-save-current");
        current.textContent = data.save_location || "No folder chosen yet.";
        if (data.save_location && !data.save_writable) {
          say(saveStatus, "That folder does not look writable from here.", "warn");
        }
        var pathField = byId("vt-save-path");
        if (pathField && !pathField.value) pathField.value = data.save_location || "";
        var labelField = byId("vt-save-label");
        if (labelField && !labelField.value) labelField.value = data.save_label || "";

        renderIps(data.ips || []);
        byId("vt-ip-cap").textContent =
          "Up to " + data.ip_history_cap + " addresses are remembered. When that fills, the " +
          "least recently seen addresses that never signed in and are not allowed to write " +
          "are dropped first — an allowed or named device is never removed.";
        renderNotices(data.notices || []);
        return data;
      }).catch(function (error) { say(saveStatus, error.message, "deny"); });
    }

    // --- account
    var accountForm = byId("vt-account-form");
    if (accountForm) {
      accountForm.addEventListener("submit", function (event) {
        event.preventDefault();
        say(accountStatus, "Updating…");
        postJson("/vt/api/settings/account", {
          current_password: byId("vt-account-current").value,
          username: byId("vt-account-username").value,
          password: byId("vt-account-password").value
        })
          .then(function (body) {
            say(accountStatus, body.message, "good");
            byId("vt-account-current").value = "";
            byId("vt-account-password").value = "";
            load();
          })
          .catch(function (error) { say(accountStatus, error.message, "deny"); });
      });
    }

    // --- save location
    var saveForm = byId("vt-save-form");
    if (saveForm) {
      saveForm.addEventListener("submit", function (event) {
        event.preventDefault();
        say(saveStatus, "Checking that folder…");
        postJson("/vt/api/settings/save-location", { path: byId("vt-save-path").value })
          .then(function (body) {
            say(saveStatus, body.message, "good");
            byId("vt-save-current").textContent = body.save_location;
          })
          // The overlap refusal explains itself in plain language; show it as-is.
          .catch(function (error) { say(saveStatus, error.message, "deny"); });
      });
    }

    var labelApply = byId("vt-save-label-apply");
    if (labelApply) {
      labelApply.addEventListener("click", function () {
        postJson("/vt/api/settings/save-label", { label: byId("vt-save-label").value })
          .then(function () {
            say(saveStatus, "Other devices will see that name instead of the folder path.",
                "good");
          })
          .catch(function (error) { say(saveStatus, error.message, "deny"); });
      });
    }

    var browseButton = byId("vt-save-browse");
    var browser = byId("vt-save-browser");
    if (browseButton) {
      browseButton.addEventListener("click", function () {
        browser.hidden = !browser.hidden;
        if (!browser.hidden) browse("");
      });
    }

    function browse(dir) {
      vtFetch("/vt/api/settings/browse?dir=" + encodeURIComponent(dir || ""))
        .then(function (listing) {
          byId("vt-browser-dir").textContent = listing.dir;
          var list = byId("vt-browser-list");
          list.innerHTML = "";

          var add = function (glyph, label, onPick) {
            var item = document.createElement("li");
            var button = document.createElement("button");
            button.type = "button";
            var icon = document.createElement("span");
            icon.setAttribute("aria-hidden", "true");
            icon.innerHTML = glyph;
            icon.style.width = "20px";
            var name = document.createElement("span");
            name.textContent = label;
            button.appendChild(icon);
            button.appendChild(name);
            button.addEventListener("click", onPick);
            item.appendChild(button);
            list.appendChild(item);
          };

          listing.shortcuts.forEach(function (shortcut) {
            add(ICONS.folder, shortcut.name, function () { browse(shortcut.path); });
          });
          if (listing.parent) {
            add(ICONS.up, "..", function () { browse(listing.parent); });
          }
          listing.folders.forEach(function (folder) {
            add(ICONS.folder, folder.name, function () { browse(folder.path); });
          });

          var use = document.createElement("li");
          var button = document.createElement("button");
          button.type = "button";
          button.textContent = listing.eligible.ok
            ? "Use this folder"
            : "Cannot use this folder";
          button.disabled = !listing.eligible.ok;
          button.addEventListener("click", function () {
            byId("vt-save-path").value = listing.dir;
            saveForm.dispatchEvent(new Event("submit"));
            browser.hidden = true;
          });
          use.appendChild(button);
          if (!listing.eligible.ok) {
            var why = document.createElement("p");
            why.className = "vt-hint";
            why.textContent = listing.eligible.reason;
            use.appendChild(why);
          }
          list.insertBefore(use, list.firstChild);
        })
        .catch(function (error) { say(saveStatus, error.message, "deny"); });
    }

    // --- connected devices
    function renderIps(rows) {
      var list = byId("vt-ip-list");
      list.innerHTML = "";
      if (!rows.length) {
        var empty = document.createElement("li");
        empty.className = "vt-empty";
        empty.textContent = "No devices have signed in yet.";
        list.appendChild(empty);
        return;
      }

      rows.forEach(function (row) {
        var item = document.createElement("li");
        item.className = "vt-ip-row";
        item.setAttribute("data-pending", row.access_requested_at ? "1" : "0");

        var head = document.createElement("div");
        head.className = "vt-ip-head";
        var address = document.createElement("span");
        address.className = "vt-ip-address";
        address.textContent = row.ip + (row.label ? "  ·  " + row.label : "");
        head.appendChild(address);

        var toggle = document.createElement("label");
        toggle.className = "vt-switch";
        var checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = !!row.write_allowed;
        var track = document.createElement("span");
        track.className = "vt-switch-track";
        var caption = document.createElement("span");
        caption.className = "vt-switch-label";
        // The word carries the state as well as the shape does.
        caption.textContent = row.write_allowed ? "Writes allowed" : "Writes blocked";
        checkbox.setAttribute("aria-label", "Allow writes from " + row.ip);
        checkbox.addEventListener("change", function () {
          postJson("/vt/api/settings/ip", {
            ip: row.ip, write_allowed: checkbox.checked
          })
            .then(function () {
              caption.textContent = checkbox.checked ? "Writes allowed" : "Writes blocked";
              load();
            })
            .catch(function (error) {
              checkbox.checked = !checkbox.checked;
              say(saveStatus, error.message, "deny");
            });
        });
        toggle.appendChild(checkbox);
        toggle.appendChild(track);
        toggle.appendChild(caption);
        head.appendChild(toggle);
        item.appendChild(head);

        if (row.access_requested_at) {
          var pending = document.createElement("p");
          pending.className = "vt-ip-pending";
          pending.textContent = "Access requested " + fmtTime(row.access_requested_at) +
            (row.access_requested_by ? " by " + row.access_requested_by : "");
          item.appendChild(pending);
        }

        var meta = document.createElement("p");
        meta.className = "vt-ip-meta";
        meta.textContent =
          "First seen " + fmtTime(row.first_seen) +
          "  ·  last seen " + fmtTime(row.last_seen) +
          "  ·  last sign-in " + fmtTime(row.last_success) +
          "  ·  " + row.successful_logins + " successful, " +
          row.failed_logins + " failed";
        item.appendChild(meta);

        list.appendChild(item);
      });
    }

    // --- maintenance notices
    function renderNotices(rows) {
      var list = byId("vt-notice-list");
      list.innerHTML = "";
      if (!rows.length) {
        var empty = document.createElement("li");
        empty.className = "vt-empty";
        empty.textContent = "Nothing to report.";
        list.appendChild(empty);
        return;
      }
      rows.forEach(function (row) {
        var item = document.createElement("li");
        item.className = "vt-notice";
        var text = document.createElement("p");
        var code = document.createElement("code");
        code.textContent = row.output_dir + "/" + row.basename;
        text.appendChild(document.createTextNode("Possible partial file from " +
          fmtTime(row.created_at) + ": "));
        text.appendChild(code);
        item.appendChild(text);

        var explain = document.createElement("p");
        explain.className = "vt-hint";
        explain.textContent =
          "Video Trim cannot prove it created this file, so it will never delete, " +
          "repair, or overwrite it. Check it yourself, then clear this note.";
        item.appendChild(explain);

        var ack = document.createElement("button");
        ack.type = "button";
        ack.className = "vt-button";
        ack.textContent = "Clear this note";
        ack.addEventListener("click", function () {
          postJson("/vt/api/settings/notice/" + row.id + "/acknowledge")
            .then(load)
            .catch(function (error) { say(saveStatus, error.message, "deny"); });
        });
        item.appendChild(ack);
        list.appendChild(item);
      });
    }

    load();
  }

  // --- dispatch -------------------------------------------------------------
  wireSignOut();
  if (view === "home") renderHome();
  else if (view === "transfer") renderTransfer();
  else if (view === "settings") renderSettings();

  window.VideoTrimShell = { fetch: vtFetch, csrf: function () { return cookie("vt_csrf"); } };
})();
