/* Files — a browser for the folder the host chose to save into.
 *
 * The listing, the picture viewer and the player all live on one page, so
 * opening a video is a swap rather than a navigation: the way back is a button,
 * the folder you were in is still there, and nothing reloads.
 *
 * Everything this file handles is named *relative to the save folder*. There is
 * no absolute path in the data it receives — the only place a real path can
 * appear is the folder's own label, and only when the server decided this
 * session may be told one. So nothing here needs to be careful about leaking a
 * path, because nothing here is ever given one to leak.
 *
 * It reads. It does not rename, move, delete or upload, and there is no route
 * behind this page that would let it.
 */
(function () {
  "use strict";

  if (document.body.getAttribute("data-view") !== "files") return;

  var PREFS_KEY = "vt.files.prefs";

  var ICONS = {
    folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.5h7A1.5 1.5 0 0 1 19 10v7.5A1.5 1.5 0 0 1 17.5 19h-13A1.5 1.5 0 0 1 3 17.5z"/></svg>',
    video: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="5" width="19" height="14" rx="2.6"/><path d="M10 9.2l5 2.8-5 2.8z"/></svg>',
    image: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="15" rx="2.6"/><circle cx="8.8" cy="9.8" r="1.7"/><path d="M3.6 17.5l4.9-4.6 3.6 3.2 3.1-2.6 5.2 4.4"/></svg>',
    audio: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 17V5.6l10-1.8V15"/><circle cx="6.4" cy="17.4" r="2.6"/><circle cx="16.4" cy="15.4" r="2.6"/></svg>',
    file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3.5h7.5L18.5 8v12.5h-12.5z"/><path d="M13.2 3.7V8.2h4.9"/></svg>',
    up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V6"/><path d="M6 12l6-6 6 6"/></svg>',
    columns: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="15" rx="2.2"/><path d="M9.5 4.5v15M15 4.5v15"/></svg>',
    order: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7 4.5v15M7 19.5l-3-3M7 19.5l3-3"/><path d="M17 19.5v-15M17 4.5l-3 3M17 4.5l3 3"/></svg>'
  };

  // Every category the listing can hold, and the words for them. Order is the
  // order of the filter chips.
  var CATEGORIES = [
    { key: "all", label: "All" },
    { key: "folder", label: "Folders" },
    { key: "video", label: "Videos" },
    { key: "image", label: "Pictures" },
    { key: "audio", label: "Audio" },
    { key: "file", label: "Other" }
  ];

  /* The Details columns. "Name" is always there — a file browser without names
   * is a puzzle — and the rest are the customizable part. Each one is also a
   * sort key, so a column you added is a column you can order by. */
  var COLUMNS = [
    { key: "name", label: "Name", fixed: true, align: "left" },
    { key: "type", label: "Type", align: "left" },
    { key: "size", label: "Size", align: "right" },
    { key: "modified", label: "Date modified", align: "right" },
    { key: "ext", label: "Extension", align: "left" },
    { key: "kind", label: "Category", align: "left" }
  ];

  var VIEWS = [
    { key: "details", label: "Details" },
    { key: "small", label: "Small" },
    { key: "medium", label: "Medium" },
    { key: "large", label: "Large" },
    { key: "huge", label: "Extra large" }
  ];

  // Poster width asked of the server per view, so a small grid does not decode
  // 720-pixel frames it will draw at 90.
  var POSTER_WIDTH = { small: 160, medium: 240, large: 360, huge: 480, details: 160 };

  var prefs = {
    view: "medium",
    sort: "name",
    order: "asc",
    filter: "all",
    columns: ["type", "size", "modified"]
  };

  var st = {
    path: "",
    entries: [],
    shown: [],
    rootLabel: "",
    canPlay: true,
    lightboxIndex: -1,
    lightboxList: []
  };

  var dom = {};

  // --- helpers ---------------------------------------------------------------
  function byId(id) { return document.getElementById(id); }

  function fmtSize(bytes) {
    if (!bytes) return "";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var index = 0;
    var value = bytes;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return (value >= 10 || index === 0 ? Math.round(value) : value.toFixed(1)) +
      " " + units[index];
  }

  function fmtWhen(seconds) {
    if (!seconds) return "";
    try { return new Date(seconds * 1000).toLocaleString(); }
    catch (err) { return ""; }
  }

  function say(message, tone) {
    dom.status.textContent = message || "";
    if (tone) dom.status.setAttribute("data-tone", tone);
    else dom.status.removeAttribute("data-tone");
  }

  function loadPrefs() {
    try {
      var saved = JSON.parse(window.localStorage.getItem(PREFS_KEY) || "null");
      if (!saved || typeof saved !== "object") return;
      if (VIEWS.some(function (v) { return v.key === saved.view; })) prefs.view = saved.view;
      if (COLUMNS.some(function (c) { return c.key === saved.sort; })) prefs.sort = saved.sort;
      if (saved.order === "asc" || saved.order === "desc") prefs.order = saved.order;
      if (CATEGORIES.some(function (c) { return c.key === saved.filter; })) {
        prefs.filter = saved.filter;
      }
      if (Array.isArray(saved.columns)) {
        prefs.columns = saved.columns.filter(function (key) {
          return COLUMNS.some(function (c) { return c.key === key && !c.fixed; });
        });
      }
    } catch (err) { /* a corrupt preference is not worth an empty page */ }
  }

  function storePrefs() {
    try { window.localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); }
    catch (err) { /* private browsing keeps the choice for this session only */ }
  }

  function fileUrl(entry, download) {
    return "/vt/library/file?path=" + encodeURIComponent(entry.path) +
      (download ? "&download=1" : "");
  }

  function posterUrl(entry) {
    return "/vt/library/poster?path=" + encodeURIComponent(entry.path) +
      "&width=" + (POSTER_WIDTH[prefs.view] || 240);
  }

  // --- the listing -----------------------------------------------------------
  function load(path) {
    say("Reading…");
    return window.VideoTrimShell.fetch(
      "/vt/api/library/list?path=" + encodeURIComponent(path || "")
    )
      .then(function (listing) {
        st.path = listing.path || "";
        st.entries = listing.entries || [];
        st.rootLabel = listing.root_label || "";
        st.canPlay = listing.can_play !== false;
        dom.where.textContent = listing.root_label
          ? "Everything Video Trim saves lands in " + listing.root_label + "."
          : "";
        renderCrumbs(listing.crumbs || [], listing.parent);
        renderFilters(listing.counts || {});
        render();
        say(listing.truncated
          ? "This folder holds more than can be listed at once — only the first " +
            st.entries.length + " items are shown."
          : "");
        return listing;
      })
      .catch(function (error) {
        st.entries = [];
        render();
        say(error.message, "deny");
      });
  }

  function renderCrumbs(crumbs, parent) {
    dom.crumbs.innerHTML = "";
    if (parent !== null && parent !== undefined && st.path) {
      var up = document.createElement("button");
      up.type = "button";
      up.className = "vt-crumb vt-crumb-up";
      up.innerHTML = ICONS.up;
      up.title = "Up one folder";
      up.setAttribute("aria-label", "Up one folder");
      up.addEventListener("click", function () { load(parent); });
      dom.crumbs.appendChild(up);
    }
    crumbs.forEach(function (crumb, index) {
      if (index) {
        var sep = document.createElement("span");
        sep.className = "vt-crumb-sep";
        sep.textContent = "/";
        dom.crumbs.appendChild(sep);
      }
      var button = document.createElement("button");
      button.type = "button";
      button.className = "vt-crumb";
      button.textContent = crumb.name;
      if (index === crumbs.length - 1) button.setAttribute("aria-current", "true");
      button.addEventListener("click", function () { load(crumb.path); });
      dom.crumbs.appendChild(button);
    });
  }

  function renderFilters(counts) {
    dom.filter.innerHTML = "";
    CATEGORIES.forEach(function (category) {
      var total = category.key === "all"
        ? st.entries.length
        : (counts[category.key] || 0);
      if (category.key !== "all" && !total) return;
      var button = document.createElement("button");
      button.type = "button";
      button.className = "vt-seg-button";
      button.textContent = category.label + "  " + total;
      button.setAttribute("data-on", prefs.filter === category.key ? "1" : "0");
      button.addEventListener("click", function () {
        prefs.filter = category.key;
        storePrefs();
        renderFilters(counts);
        render();
      });
      dom.filter.appendChild(button);
    });
  }

  function renderViewButtons() {
    dom.view.innerHTML = "";
    VIEWS.forEach(function (mode) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "vt-seg-button";
      button.textContent = mode.label;
      button.setAttribute("data-on", prefs.view === mode.key ? "1" : "0");
      button.addEventListener("click", function () {
        prefs.view = mode.key;
        storePrefs();
        renderViewButtons();
        render();
      });
      dom.view.appendChild(button);
    });
  }

  function renderSortChoices() {
    dom.sort.innerHTML = "";
    COLUMNS.forEach(function (column) {
      var option = document.createElement("option");
      option.value = column.key;
      option.textContent = column.label;
      dom.sort.appendChild(option);
    });
    dom.sort.value = prefs.sort;
    dom.order.innerHTML = ICONS.order;
    dom.order.setAttribute("data-order", prefs.order);
    dom.order.title = prefs.order === "asc" ? "Ascending — click for descending"
                                            : "Descending — click for ascending";
  }

  function renderColumnMenu() {
    dom.columnsList.innerHTML = "";
    COLUMNS.forEach(function (column) {
      var row = document.createElement("label");
      row.className = "vt-column-choice";
      var box = document.createElement("input");
      box.type = "checkbox";
      box.checked = column.fixed || prefs.columns.indexOf(column.key) >= 0;
      box.disabled = !!column.fixed;
      box.addEventListener("change", function () {
        prefs.columns = COLUMNS.filter(function (candidate) {
          if (candidate.fixed) return false;
          if (candidate.key === column.key) return box.checked;
          return prefs.columns.indexOf(candidate.key) >= 0;
        }).map(function (candidate) { return candidate.key; });
        storePrefs();
        render();
      });
      var text = document.createElement("span");
      text.textContent = column.label + (column.fixed ? " (always shown)" : "");
      row.appendChild(box);
      row.appendChild(text);
      dom.columnsList.appendChild(row);
    });
  }

  function activeColumns() {
    return COLUMNS.filter(function (column) {
      return column.fixed || prefs.columns.indexOf(column.key) >= 0;
    });
  }

  function sorted(entries) {
    var direction = prefs.order === "desc" ? -1 : 1;
    var key = prefs.sort;
    return entries.slice().sort(function (left, right) {
      // Folders lead, whichever column is sorted — they are the way through the
      // tree, not one more row of it.
      if ((left.kind === "folder") !== (right.kind === "folder")) {
        return left.kind === "folder" ? -1 : 1;
      }
      var a = left[key];
      var b = right[key];
      if (key === "size" || key === "modified") {
        return ((a || 0) - (b || 0)) * direction;
      }
      a = String(a === undefined ? "" : a).toLowerCase();
      b = String(b === undefined ? "" : b).toLowerCase();
      if (a === b) return left.name.toLowerCase() < right.name.toLowerCase() ? -1 : 1;
      return (a < b ? -1 : 1) * direction;
    });
  }

  function filtered() {
    if (prefs.filter === "all") return st.entries;
    return st.entries.filter(function (entry) { return entry.kind === prefs.filter; });
  }

  function render() {
    st.shown = sorted(filtered());
    dom.body.setAttribute("data-view", prefs.view);
    dom.body.innerHTML = "";

    if (!st.shown.length) {
      var empty = document.createElement("p");
      empty.className = "vt-empty";
      empty.textContent = st.entries.length
        ? "Nothing in this folder matches that filter."
        : "This folder is empty.";
      dom.body.appendChild(empty);
      return;
    }

    if (prefs.view === "details") renderDetails();
    else renderTiles();
  }

  function renderDetails() {
    var columns = activeColumns();
    var table = document.createElement("table");
    table.className = "vt-files-table";

    var head = document.createElement("thead");
    var headRow = document.createElement("tr");
    columns.forEach(function (column) {
      var cell = document.createElement("th");
      cell.setAttribute("data-align", column.align);
      var button = document.createElement("button");
      button.type = "button";
      button.className = "vt-sort-head";
      button.textContent = column.label;
      if (prefs.sort === column.key) {
        button.setAttribute("data-sorted", prefs.order);
        cell.setAttribute("aria-sort",
          prefs.order === "asc" ? "ascending" : "descending");
      }
      button.addEventListener("click", function () {
        if (prefs.sort === column.key) {
          prefs.order = prefs.order === "asc" ? "desc" : "asc";
        } else {
          prefs.sort = column.key;
          prefs.order = "asc";
        }
        storePrefs();
        renderSortChoices();
        render();
      });
      cell.appendChild(button);
      headRow.appendChild(cell);
    });
    head.appendChild(headRow);
    table.appendChild(head);

    var body = document.createElement("tbody");
    st.shown.forEach(function (entry) {
      var row = document.createElement("tr");
      row.className = "vt-file-row";
      row.setAttribute("data-kind", entry.kind);
      row.tabIndex = 0;
      columns.forEach(function (column) {
        var cell = document.createElement("td");
        cell.setAttribute("data-align", column.align);
        if (column.key === "name") {
          var icon = document.createElement("i");
          icon.className = "vt-file-icon";
          icon.innerHTML = ICONS[entry.kind] || ICONS.file;
          var label = document.createElement("span");
          label.textContent = entry.name;
          cell.className = "vt-file-name";
          cell.appendChild(icon);
          cell.appendChild(label);
        } else if (column.key === "size") {
          cell.textContent = entry.kind === "folder" ? "" : fmtSize(entry.size);
        } else if (column.key === "modified") {
          cell.textContent = fmtWhen(entry.modified);
        } else {
          cell.textContent = entry[column.key] || "";
        }
        row.appendChild(cell);
      });
      row.addEventListener("click", function () { activate(entry); });
      row.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activate(entry);
        }
      });
      body.appendChild(row);
    });
    table.appendChild(body);
    dom.body.appendChild(table);
  }

  function renderTiles() {
    var grid = document.createElement("div");
    grid.className = "vt-tiles";

    st.shown.forEach(function (entry) {
      var tile = document.createElement("button");
      tile.type = "button";
      tile.className = "vt-tile";
      tile.setAttribute("data-kind", entry.kind);

      var frame = document.createElement("span");
      frame.className = "vt-tile-thumb";
      frame.innerHTML = ICONS[entry.kind] || ICONS.file;

      /* Pictures are drawn from the file itself; a video gets a poster frame
       * rendered on the host. Either way a failure quietly leaves the glyph
       * that is already there rather than a broken image. */
      if (entry.kind === "image") {
        addThumb(frame, fileUrl(entry), entry.name);
      } else if (entry.kind === "video" && st.canPlay) {
        addThumb(frame, posterUrl(entry), entry.name);
      }

      var name = document.createElement("span");
      name.className = "vt-tile-name";
      name.textContent = entry.name;
      name.title = entry.name;

      var meta = document.createElement("span");
      meta.className = "vt-tile-meta";
      meta.textContent = entry.kind === "folder"
        ? "Folder"
        : (fmtSize(entry.size) + (entry.type ? "  ·  " + entry.type : ""));

      tile.appendChild(frame);
      tile.appendChild(name);
      tile.appendChild(meta);
      tile.addEventListener("click", function () { activate(entry); });
      grid.appendChild(tile);
    });

    dom.body.appendChild(grid);
  }

  /* The picture is put in the tile straight away, on top of the glyph that is
   * already there, and only *revealed* once it has loaded — so a folder of two
   * hundred videos scrolls at once and fills in as the posters arrive. It has to
   * be in the document for that: a detached image with loading="lazy" is never
   * in a viewport, so it never loads at all. */
  function addThumb(frame, url, alt) {
    var image = new Image();
    image.loading = "lazy";
    image.decoding = "async";
    image.alt = alt || "";
    image.addEventListener("load", function () {
      frame.setAttribute("data-thumb", "1");
    });
    image.addEventListener("error", function () {
      if (image.parentNode) image.parentNode.removeChild(image);
    });
    frame.appendChild(image);
    image.src = url;
  }

  // --- opening things --------------------------------------------------------
  function activate(entry) {
    if (entry.kind === "folder") {
      load(entry.path);
      return;
    }
    if (entry.kind === "image") {
      openLightbox(entry);
      return;
    }
    if (entry.kind === "video") {
      openInPlayer(entry);
      return;
    }
    say("Video Trim opens pictures and videos from this folder. " +
        entry.name + " is neither, so it is only listed.");
  }

  function openInPlayer(entry) {
    if (!window.VideoTrim || !window.VideoTrim.openLibrary) {
      say("The player did not load. Reload the page and try again.", "deny");
      return;
    }
    showPlayer(entry.name);
    window.VideoTrim.openLibrary(entry.path, entry.name).catch(function () {
      // The player has already said what went wrong in its own toast; all this
      // has to do is not strand somebody on a blank player.
      showFiles();
    });
  }

  function showPlayer(name) {
    dom.playerName.textContent = name || "";
    dom.playerShell.hidden = false;
    dom.filesMain.hidden = true;
    document.body.setAttribute("data-mode", "player");
    if (window.VideoTrim && window.VideoTrim.mount) window.VideoTrim.mount();
    window.scrollTo(0, 0);
  }

  function showFiles() {
    if (window.VideoTrim) {
      if (window.VideoTrim.closeOptions) window.VideoTrim.closeOptions();
      if (window.VideoTrim.pause) window.VideoTrim.pause();
    }
    dom.playerShell.hidden = true;
    dom.filesMain.hidden = false;
    document.body.removeAttribute("data-mode");
  }

  // --- the picture viewer ----------------------------------------------------
  function openLightbox(entry) {
    st.lightboxList = st.shown.filter(function (item) { return item.kind === "image"; });
    st.lightboxIndex = st.lightboxList.indexOf(entry);
    if (st.lightboxIndex < 0) {
      st.lightboxList = [entry];
      st.lightboxIndex = 0;
    }
    showLightbox();
  }

  function showLightbox() {
    var entry = st.lightboxList[st.lightboxIndex];
    if (!entry) return;
    dom.lightbox.hidden = false;
    dom.lightboxName.textContent = entry.name + "  ·  " + fmtSize(entry.size);
    dom.lightboxImage.src = fileUrl(entry);
    dom.lightboxImage.alt = entry.name;
    dom.lightboxDownload.href = fileUrl(entry, true);
    dom.lightboxDownload.setAttribute("download", entry.name);
    var many = st.lightboxList.length > 1;
    dom.lightboxPrev.hidden = !many;
    dom.lightboxNext.hidden = !many;
  }

  function stepLightbox(delta) {
    if (!st.lightboxList.length) return;
    st.lightboxIndex =
      (st.lightboxIndex + delta + st.lightboxList.length) % st.lightboxList.length;
    showLightbox();
  }

  function closeLightbox() {
    dom.lightbox.hidden = true;
    dom.lightboxImage.removeAttribute("src");
  }

  // --- wiring ----------------------------------------------------------------
  function collect() {
    dom.filesMain = byId("vt-files");
    dom.crumbs = byId("vt-crumbs");
    dom.where = byId("vt-files-where");
    dom.filter = byId("vt-files-filter");
    dom.view = byId("vt-files-view");
    dom.sort = byId("vt-files-sort");
    dom.order = byId("vt-files-order");
    dom.columnsToggle = byId("vt-files-columns-toggle");
    dom.columns = byId("vt-files-columns");
    dom.columnsList = byId("vt-files-columns-list");
    dom.body = byId("vt-files-body");
    dom.status = byId("vt-files-status");

    dom.lightbox = byId("vt-lightbox");
    dom.lightboxImage = byId("vt-lightbox-image");
    dom.lightboxName = byId("vt-lightbox-name");
    dom.lightboxDownload = byId("vt-lightbox-download");
    dom.lightboxPrev = byId("vt-lightbox-prev");
    dom.lightboxNext = byId("vt-lightbox-next");
    dom.lightboxClose = byId("vt-lightbox-close");

    dom.playerShell = byId("vt-player-shell");
    dom.playerName = byId("vt-player-name");
    dom.playerBack = byId("vt-player-back");
    return !!(dom.filesMain && dom.body);
  }

  function wire() {
    dom.sort.addEventListener("change", function () {
      prefs.sort = dom.sort.value;
      storePrefs();
      render();
    });
    dom.order.addEventListener("click", function () {
      prefs.order = prefs.order === "asc" ? "desc" : "asc";
      storePrefs();
      renderSortChoices();
      render();
    });
    dom.columnsToggle.innerHTML = ICONS.columns;
    dom.columnsToggle.addEventListener("click", function () {
      dom.columns.hidden = !dom.columns.hidden;
      dom.columnsToggle.setAttribute("aria-expanded", dom.columns.hidden ? "false" : "true");
    });

    dom.playerBack.addEventListener("click", showFiles);
    dom.lightboxClose.addEventListener("click", closeLightbox);
    dom.lightboxPrev.addEventListener("click", function () { stepLightbox(-1); });
    dom.lightboxNext.addEventListener("click", function () { stepLightbox(1); });
    dom.lightbox.addEventListener("click", function (event) {
      if (event.target === dom.lightbox) closeLightbox();
    });

    document.addEventListener("keydown", function (event) {
      if (!dom.lightbox.hidden) {
        if (event.key === "Escape") { closeLightbox(); event.preventDefault(); }
        if (event.key === "ArrowLeft") { stepLightbox(-1); event.preventDefault(); }
        if (event.key === "ArrowRight") { stepLightbox(1); event.preventDefault(); }
        return;
      }
      // The player owns the keyboard while it is on screen; it checks that for
      // itself, so all this has to do is stay out of its way.
      if (!dom.playerShell.hidden) return;
      if (event.key === "Escape" && !dom.columns.hidden) {
        dom.columns.hidden = true;
        dom.columnsToggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  if (!collect()) return;
  loadPrefs();
  wire();
  renderViewButtons();
  renderSortChoices();
  renderColumnMenu();
  load("");
})();
