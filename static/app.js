/* Plain-WSGI taskboard client. Renders everything through textContent and
 * createElement so user data is never assigned via innerHTML (output is
 * escaped). Mutations always carry the per-session CSRF token from the boot
 * payload delivered by the server-rendered shell. */
(function () {
  "use strict";

  var boot = (window.TB_BOOT || {}).csrf
    ? window.TB_BOOT
    : { csrf: "", release: "", base: "", runtime: "" };

  var app = document.getElementById("app");
  var statusEl = document.getElementById("status-text");

  var state = {
    projects: [],
    tasks: [],
    filters: { q: "", status: "", priority: "", project_id: "" },
    editTaskId: null,
    editing: false,
  };

  function api(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers["X-CSRF-Token"] = boot.csrf;
    if (opts.body && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    return fetch(boot.base + path, opts).then(function (resp) {
      return resp
        .json()
        .catch(function () {
          return {};
        })
        .then(function (payload) {
          if (!resp.ok) {
            var err = new Error(payload.error || ("HTTP " + resp.status));
            err.payload = payload;
            err.status = resp.status;
            throw err;
          }
          return payload;
        });
    });
  }

  function setStatus(text, isError) {
    statusEl.textContent = text;
    statusEl.style.color = isError ? "#c0392b" : "";
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function loadAll() {
    var ok = Promise.all([
      api("/api/projects"),
      api("/api/tasks?" + qs(state.filters)),
    ]);
    return ok.then(function (results) {
      state.projects = results[0].projects || [];
      state.tasks = results[1].tasks || [];
      render();
    });
  }

  function qs(filters) {
    var parts = [];
    Object.keys(filters).forEach(function (key) {
      var value = filters[key];
      if (value) parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(value));
    });
    return parts.join("&");
  }

  function projectName(id) {
    for (var i = 0; i < state.projects.length; i++) {
      if (String(state.projects[i].id) === String(id)) return state.projects[i].name;
    }
    return "project " + id;
  }

  function render() {
    app.textContent = "";
    app.appendChild(toolbar());
    app.appendChild(forms());
    app.appendChild(columns());
  }

  function toolbar() {
    var bar = el("section", "toolbar");

    var search = el("input", "fill");
    search.type = "search";
    search.placeholder = "Search tasks…";
    search.value = state.filters.q || "";
    search.addEventListener("input", function () {
      state.filters.q = search.value;
    });
    search.addEventListener("change", loadAll);

    var statusSelect = select(
      [["", "any status"], ["todo", "todo"], ["in_progress", "in progress"], ["done", "done"]],
      state.filters.status,
      function (value) { state.filters.status = value; loadAll(); }
    );

    var prioritySelect = select(
      [["", "any priority"], ["low", "low"], ["medium", "medium"], ["high", "high"]],
      state.filters.priority,
      function (value) { state.filters.priority = value; loadAll(); }
    );

    var projectSelect = select(
      projectOptions(),
      state.filters.project_id,
      function (value) { state.filters.project_id = value; loadAll(); }
    );

    bar.appendChild(search);
    bar.appendChild(statusSelect);
    bar.appendChild(prioritySelect);
    bar.appendChild(projectSelect);
    bar.appendChild(button("Reset", null, function () {
      state.filters = { q: "", status: "", priority: "", project_id: "" };
      search.value = "";
      loadAll();
    }));
    return bar;
  }

  function projectOptions() {
    var options = [["", "all projects"]];
    state.projects.forEach(function (p) {
      options.push([String(p.id), p.name]);
    });
    return options;
  }

  function select(options, current, onChange) {
    var node = el("select");
    options.forEach(function (pair) {
      var opt = el("option", null, pair[1]);
      opt.value = pair[0];
      node.appendChild(opt);
    });
    node.value = current || "";
    node.addEventListener("change", function () { onChange(node.value); });
    return node;
  }

  function button(text, cssClass, onClick) {
    var node = el("button", cssClass, text);
    node.addEventListener("click", onClick);
    return node;
  }

  function forms() {
    var wrap = el("section", "forms");

    var projectForm = el("form", "card");
    projectForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var data = {
        name: formValue(projectForm, "prj-name"),
        description: formValue(projectForm, "prj-desc"),
        status: formValue(projectForm, "prj-status"),
      };
      api("/api/projects", { method: "POST", body: data })
        .then(function () {
          projectForm.reset();
          setStatus("project created");
          return loadAll();
        })
        .catch(function (err) { setStatus(err.message, true); });
    });
    projectForm.appendChild(el("h2", null, "New project"));
    projectForm.appendChild(field("Name", textInput("prj-name", "")));
    projectForm.appendChild(field("Description", textInput("prj-desc", "")));
    projectForm.appendChild(field("Status", select(
      [["active", "active"], ["archived", "archived"]], "active", function () {}), "prj-status"));
    projectForm.appendChild(submitButton("Create project"));

    var taskForm = el("form", "card");
    taskForm.id = "task-form";
    taskForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var method = state.editing ? "PATCH" : "POST";
      var path = state.editing ? "/api/tasks/" + state.editTaskId : "/api/tasks";
      var data = {
        project_id: formValue(taskForm, "task-project"),
        title: formValue(taskForm, "task-title"),
        description: formValue(taskForm, "task-desc"),
        status: formValue(taskForm, "task-status"),
        priority: formValue(taskForm, "task-priority"),
      };
      api(path, { method: method, body: data })
        .then(function () {
          taskForm.reset();
          state.editing = false;
          state.editTaskId = null;
          setStatus(method === "PATCH" ? "task updated" : "task created");
          return loadAll();
        })
        .catch(function (err) { setStatus(err.message, true); });
    });
    taskForm.appendChild(el("h2", null, "New task"));
    taskForm.appendChild(field("Project", select(projectOptions(), "", function () {}), "task-project"));
    taskForm.appendChild(field("Title", textInput("task-title", "")));
    taskForm.appendChild(field("Description", textInput("task-desc", "")));
    taskForm.appendChild(field("Status", select(
      [["todo", "todo"], ["in_progress", "in progress"], ["done", "done"]],
      "todo", function () {}), "task-status"));
    taskForm.appendChild(field("Priority", select(
      [["low", "low"], ["medium", "medium"], ["high", "high"]],
      "medium", function () {}), "task-priority"));
    taskForm.appendChild(submitButton("Create task"));

    wrap.appendChild(projectForm);
    wrap.appendChild(taskForm);
    return wrap;
  }

  function field(labelText, input, name) {
    if (name) input.name = name;
    var label = el("label", "field");
    label.appendChild(el("span", "field-label", labelText));
    label.appendChild(input);
    return label;
  }

  function textInput(name, value) {
    var node = document.createElement("input");
    node.type = "text";
    node.name = name;
    node.value = value;
    return node;
  }

  function submitButton(text) {
    var node = el("button", "primary", text);
    node.type = "submit";
    return node;
  }

  function formValue(form, name) {
    var input = form.querySelector("[name='" + name + "']");
    return input ? input.value : "";
  }

  function columns() {
    var wrap = el("section", "columns");

    var projectList = el("div", "column");
    projectList.appendChild(el("h2", null, "Projects (" + state.projects.length + ")"));
    state.projects.forEach(function (p) {
      var card = el("article", "project-card");
      card.appendChild(el("h3", null, p.name + " · " + p.status));
      card.appendChild(el("p", "muted", (p.description || "") + " — " + p.open_count + " open of " + p.task_count));
      card.appendChild(button("Edit", null, function () { promptEditProject(p); }));
      card.appendChild(button("Delete", "danger", function () { deleteProject(p.id); }));
      projectList.appendChild(card);
    });

    var taskList = el("div", "column");
    taskList.appendChild(el("h2", null, "Tasks (" + state.tasks.length + ")"));
    if (!state.tasks.length) {
      taskList.appendChild(el("p", "muted", "no tasks match the current filters"));
    }
    state.tasks.forEach(function (t) {
      var item = el("article", "task-item");
      var head = el("div", "task-head");
      head.appendChild(el("span", "task-title " + t.status, t.title));
      var badge = el("span", "badge", t.status + " · " + t.priority);
      head.appendChild(badge);
      item.appendChild(head);
      item.appendChild(el("p", "muted", projectName(t.project_id) + (t.description ? " — " + t.description : "")));
      var actions = el("div", "actions");
      actions.appendChild(statusSelect(t.id, t.status));
      actions.appendChild(button("Edit", null, function () { promptEditTask(t); }));
      actions.appendChild(button("Delete", "danger", function () { deleteTask(t.id); }));
      item.appendChild(actions);
      taskList.appendChild(item);
    });

    wrap.appendChild(projectList);
    wrap.appendChild(taskList);
    return wrap;
  }

  function statusSelect(id, current) {
    var node = select(
      [["todo", "todo"], ["in_progress", "in progress"], ["done", "done"]],
      current,
      function (value) {
        api("/api/tasks/" + id, { method: "PATCH", body: { status: value } })
          .then(loadAll)
          .catch(function (err) { setStatus(err.message, true); });
      }
    );
    return node;
  }

  function promptEditProject(p) {
    var name = window.prompt("Project name", p.name);
    if (name === null) return;
    api("/api/projects/" + p.id, { method: "PATCH", body: { name: name } })
      .then(function () { setStatus("project updated"); return loadAll(); })
      .catch(function (err) { setStatus(err.message, true); });
  }

  function promptEditTask(t) {
    var title = window.prompt("Task title", t.title);
    if (title === null) return;
    api("/api/tasks/" + t.id, { method: "PATCH", body: { title: title } })
      .then(function () { setStatus("task updated"); return loadAll(); })
      .catch(function (err) { setStatus(err.message, true); });
  }

  function deleteProject(id) {
    if (!window.confirm("Delete this project and all its tasks?")) return;
    api("/api/projects/" + id, { method: "DELETE" })
      .then(function () { setStatus("project deleted"); return loadAll(); })
      .catch(function (err) { setStatus(err.message, true); });
  }

  function deleteTask(id) {
    if (!window.confirm("Delete this task?")) return;
    api("/api/tasks/" + id, { method: "DELETE" })
      .then(function () { setStatus("task deleted"); return loadAll(); })
      .catch(function (err) { setStatus(err.message, true); });
  }

  function updateStatus(id, status) {
    api("/api/tasks/" + id, { method: "PATCH", body: { status: status } })
      .then(function () { setStatus("task status updated"); return loadAll(); })
      .catch(function (err) { setStatus(err.message, true); });
  }

  var deepLink = window.location.pathname.match(/\/project\/(\d+)/);
  if (deepLink) {
    state.filters.project_id = deepLink[1];
  }

  loadAll().then(function () {
    return api("/api/health/ready");
  }).then(function () {
    setStatus("ready — release " + boot.release);
  }).catch(function () {
    setStatus("ready — release " + boot.release);
  });

  window.PB = { loadAll: loadAll, updateStatus: updateStatus };
})();