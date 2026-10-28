(function () {
  "use strict";

  var POLL_MS = 15000;
  var SHIFT_MS = 60000;
  var SHIFTS = [[0, 0], [6, 0], [6, 6], [0, 6], [-6, 6], [-6, 0], [-6, -6], [0, -6], [6, -6]];

  var OVERALL = {
    ok: "Tudo certo",
    warning: "Atenção",
    error: "Há problemas",
    unavailable: "Sem dados"
  };
  var PROBLEMS = {
    unhealthy: "não saudável",
    restart_loop: "reiniciando em loop",
    oom_killed: "sem memória (OOM)",
    exited_with_error: "parou com erro",
    dead: "morto"
  };
  var DEVICE_TYPES = {
    router: "roteador",
    printer: "impressora",
    tv: "TV",
    media_player: "player de mídia",
    camera: "câmera",
    nas: "NAS",
    computer: "computador",
    phone_or_tablet: "celular ou tablet",
    smart_home: "casa inteligente",
    unknown: "desconhecido"
  };
  var FINDINGS = {
    new_device: "Novo dispositivo",
    gateway_mac_changed: "MAC do roteador mudou",
    duplicate_mac: "MAC duplicado",
    risky_ports: "Portas de risco"
  };
  var CHECKS = { ok: "ok", warning: "atrasado", error: "falhou" };

  var lastOk = 0;
  var shiftIndex = 0;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function number(value, digits) {
    return Number(value).toLocaleString("pt-BR", {
      minimumFractionDigits: digits || 0,
      maximumFractionDigits: digits || 0
    });
  }

  function bytes(value) {
    if (value === null || value === undefined) { return "–"; }
    var gib = value / 1073741824;
    if (gib >= 1024) { return number(gib / 1024, 1) + " TB"; }
    if (gib >= 10) { return number(gib, 0) + " GB"; }
    if (gib >= 1) { return number(gib, 1) + " GB"; }
    var mib = value / 1048576;
    return mib >= 1 ? number(mib, 0) + " MB" : "< 1 MB";
  }

  function took(seconds) {
    if (seconds === null || seconds === undefined) { return ""; }
    if (seconds < 60) { return seconds + " s"; }
    var minutes = Math.floor(seconds / 60);
    var rest = seconds % 60;
    return minutes + " min" + (rest ? " " + rest + " s" : "");
  }

  function duration(seconds) {
    var days = Math.floor(seconds / 86400);
    var hours = Math.floor((seconds % 86400) / 3600);
    if (days > 0) { return days + " d " + hours + " h"; }
    var minutes = Math.floor((seconds % 3600) / 60);
    return hours + " h " + minutes + " min";
  }

  function age(hours) {
    if (hours === null || hours === undefined) { return "sem registro"; }
    if (hours < 1) { return "há menos de 1 h"; }
    if (hours < 48) { return "há " + number(hours, 0) + " h"; }
    return "há " + number(hours / 24, 0) + " d";
  }

  function level(percent, warning, error) {
    if (percent >= error) { return "error"; }
    return percent >= warning ? "warning" : "ok";
  }

  function row(label, value) {
    var node = el("div", "row");
    node.appendChild(el("span", "label", label));
    node.appendChild(el("span", "value", value));
    return node;
  }

  function meter(label, percent, valueText, state) {
    var node = el("div", "meter");
    var head = el("div", "head");
    head.appendChild(el("span", "", label));
    head.appendChild(el("span", "value", valueText));
    node.appendChild(head);
    var bar = el("div", "bar " + state);
    var fill = el("div");
    fill.style.width = Math.max(0, Math.min(100, percent)) + "%";
    bar.appendChild(fill);
    node.appendChild(bar);
    return node;
  }

  function item(name, value, cls) {
    var node = el("div", "item" + (cls ? " " + cls : ""));
    node.appendChild(el("span", "name", name));
    node.appendChild(el("span", "value", value));
    return node;
  }

  // Elements that can go when the panel is too small: the higher the number, the sooner it goes.
  function droppable(node, priority) {
    node.setAttribute("data-drop", String(priority));
    return node;
  }

  // True when the last line of a panel ends below the panel's inner edge. It compares real positions
  // instead of trusting scrollHeight, which a clipped flex box does not always report.
  function overflows(body) {
    if (body.scrollHeight > body.clientHeight + 1) { return true; }
    var last = body.lastElementChild;
    if (!last) { return false; }
    var panelNode = body.parentNode;
    var style = window.getComputedStyle(panelNode);
    var inner = panelNode.getBoundingClientRect().bottom -
      parseFloat(style.paddingBottom) - parseFloat(style.borderBottomWidth);
    var limit = Math.min(body.getBoundingClientRect().bottom, inner);
    return last.getBoundingClientRect().bottom > limit + 1;
  }

  function fit(body) {
    var guard = 0;
    while (overflows(body) && guard < 80) {
      guard += 1;
      var nodes = body.querySelectorAll("[data-drop]");
      if (nodes.length === 0) { return; }
      var top = -1;
      var i;
      for (i = 0; i < nodes.length; i++) {
        top = Math.max(top, Number(nodes[i].getAttribute("data-drop")));
      }
      for (i = 0; i < nodes.length; i++) {
        if (Number(nodes[i].getAttribute("data-drop")) === top && nodes[i].parentNode) {
          nodes[i].parentNode.removeChild(nodes[i]);
        }
      }
      var groups = body.querySelectorAll(".cols");
      for (i = 0; i < groups.length; i++) {
        if (!groups[i].querySelector(".item")) { groups[i].parentNode.removeChild(groups[i]); }
      }
    }
  }

  function panel(id, section) {
    var node = document.getElementById(id);
    node.className = "panel s-" + section.state;
    var body = node.querySelector(".body");
    while (body.firstChild) { body.removeChild(body.firstChild); }
    return body;
  }

  function unavailable(body, section) {
    body.appendChild(el("div", "big", "Indisponível"));
    var reason = section.note || section.summary;
    if (reason) { body.appendChild(el("div", "sub", reason)); }
  }

  var DOCKER_PARTS = { images: "imagens", volumes: "volumes", build_cache: "cache de build" };
  var UPDATE_STATUS = {
    succeeded: ["ok", ""],
    failed: ["falhou", "bad"],
    rejected: ["recusado", "bad"],
    running: ["rodando", "warn"],
    queued: ["na fila", "warn"]
  };

  function renderDocker(body, docker) {
    if (!docker || docker.state === "unavailable") {
      body.appendChild(droppable(row("Docker", "indisponível"), 48));
      return;
    }
    body.appendChild(row("Docker",
      bytes(docker.total_bytes) + " em uso  ·  " + bytes(docker.reclaimable_bytes) + " recuperáveis"));
    docker.parts.forEach(function (part, index) {
      var line = row(DOCKER_PARTS[part.name] || part.name,
        bytes(part.size_bytes) + "  ·  " + bytes(part.reclaimable_bytes) + " recuperáveis");
      line.className += " sub";
      body.appendChild(droppable(line, 44 + index));
    });
  }

  function renderUpdates(body, updates) {
    if (!updates || updates.state === "unavailable") { return; }
    var block = el("div", "list");
    block.appendChild(el("h3", "", "Últimos updates"));
    if (updates.recent.length === 0) {
      block.appendChild(el("div", "sub", "Nenhum update pedido ainda"));
    }
    updates.recent.forEach(function (u, index) {
      var info = UPDATE_STATUS[u.status] || [u.status, ""];
      var line = item((u.project || "?") + "  ·  " + info[0], age(u.age_hours), info[1]);
      block.appendChild(index > 0 ? droppable(line, 35 + index) : line);
    });
    body.appendChild(droppable(block, 34));
  }

  function renderHost(section, docker, updates) {
    var body = panel("host", section);
    if (section.cpu_percent === null) { return unavailable(body, section); }
    var meters = el("div", "meters");
    meters.appendChild(meter("CPU", section.cpu_percent, number(section.cpu_percent, 0) + " %",
      level(section.cpu_percent, 85, 97)));
    meters.appendChild(meter("Memória", section.memory_percent,
      bytes(section.memory_used_bytes) + " de " + bytes(section.memory_total_bytes),
      level(section.memory_percent, 90, 97)));
    section.disks.forEach(function (disk, index) {
      var node = meter("Disco " + disk.name, disk.percent,
        number(disk.percent, 0) + " % de " + bytes(disk.total_bytes),
        level(disk.percent, 85, 95));
      meters.appendChild(index > 0 ? droppable(node, 10 + index) : node);
    });
    body.appendChild(meters);
    renderDocker(body, docker);
    renderUpdates(body, updates);
    body.appendChild(droppable(row("Ligado há", duration(section.uptime_seconds)), 50));
    if (section.note) { body.appendChild(el("div", "sub", section.note)); }
  }

  function consumers(title, rows, valueOf) {
    var column = el("div");
    column.appendChild(el("h3", "", title));
    var list = el("div", "list");
    rows.forEach(function (r, index) {
      list.appendChild(droppable(item(r.name, valueOf(r)), 40 + index));
    });
    column.appendChild(list);
    return column;
  }

  function renderContainers(section) {
    var body = panel("containers", section);
    if (section.total === 0 && section.state === "unavailable") { return unavailable(body, section); }
    var big = el("div", "big", section.running + " de " + section.total);
    big.appendChild(el("small", "", "rodando"));
    body.appendChild(big);
    if (section.problems.length === 0) {
      body.appendChild(el("div", "sub", "Nenhum container com problema"));
    } else {
      var list = el("div", "list");
      section.problems.slice(0, 3).forEach(function (p) {
        var kinds = p.problems.map(function (k) { return PROBLEMS[k] || k; }).join(", ");
        list.appendChild(item(p.name, kinds, "bad"));
      });
      if (section.problems.length > 3) {
        list.appendChild(item("+" + (section.problems.length - 3) + " outros", "", "bad"));
      }
      body.appendChild(list);
    }
    var cols = el("div", "cols");
    cols.appendChild(consumers("Mais CPU", section.top_cpu.slice(0, 4),
      function (r) { return number(r.cpu_percent, 1) + " %"; }));
    cols.appendChild(consumers("Mais memória", section.top_memory.slice(0, 4),
      function (r) { return bytes(r.memory_usage_bytes); }));
    body.appendChild(cols);
  }

  function renderBackup(section) {
    var body = panel("backup", section);
    if (section.status === null) { return unavailable(body, section); }
    var pill = el("span", "pill " + section.state, OVERALL[section.state]);
    var head = el("div", "big");
    head.appendChild(pill);
    body.appendChild(head);
    var last = age(section.last_success_age_hours);
    if (section.last_duration_seconds !== null && section.last_duration_seconds !== undefined) {
      last += "  ·  levou " + took(section.last_duration_seconds);
    }
    body.appendChild(row("Último backup", last));
    section.repositories.forEach(function (repo) {
      var text = age(repo.snapshot_age_hours);
      if (repo.size_bytes !== null && repo.size_bytes !== undefined) {
        text += "  ·  " + bytes(repo.size_bytes);
      }
      if (repo.free_bytes !== null && repo.free_bytes !== undefined) {
        text += "  ·  " + bytes(repo.free_bytes) + " livres";
      }
      body.appendChild(droppable(row("Cópia " + repo.name, text), 20));
    });
    body.appendChild(droppable(
      row("Verificação semanal", CHECKS[section.integrity] || "sem registro"), 26));
    var restore = CHECKS[section.restore_test] || "sem registro";
    if (section.restore_next_run_at) {
      var next = new Date(section.restore_next_run_at);
      if (!isNaN(next.getTime())) {
        restore += "  ·  próximo " + next.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" });
      }
    }
    body.appendChild(droppable(row("Teste de restauração", restore), 25));
    if (section.state !== "ok" && section.summary) {
      body.appendChild(droppable(el("div", "sub", section.summary), 60));
    }
  }

  function renderNetwork(section) {
    var body = panel("network", section);
    if (section.state === "unavailable") { return unavailable(body, section); }
    var big = el("div", "big", section.online);
    big.appendChild(el("small", "", "dispositivos online"));
    body.appendChild(big);
    var chips = el("div", "chips");
    section.by_type.forEach(function (t) {
      chips.appendChild(el("span", "chip", t.count + " " + (DEVICE_TYPES[t.device_type] || t.device_type)));
    });
    body.appendChild(droppable(chips, 50));
    if (section.new_devices > 0) {
      body.appendChild(droppable(el("div", "sub", section.new_devices +
        (section.new_devices === 1 ? " dispositivo novo" : " dispositivos novos") +
        " nos últimos dias"), 45));
    }
    if (section.findings.length === 0) {
      body.appendChild(el("div", "sub", "Nada incomum na rede"));
    } else {
      var list = el("div", "list");
      section.findings.slice(0, 3).forEach(function (f, index) {
        var node = item(FINDINGS[f.kind] || f.kind, f.ip || "", f.severity === "alert" ? "bad" : "warn");
        list.appendChild(index > 0 ? droppable(node, 30 + index) : node);
      });
      body.appendChild(list);
    }
    if (section.offline.length > 0) {
      var away = el("div", "list");
      away.appendChild(el("h3", "", "Fora do ar"));
      section.offline.slice(0, 3).forEach(function (d, index) {
        var line = item(d.name, (DEVICE_TYPES[d.device_type] || d.device_type) + "  ·  " + age(d.age_hours));
        away.appendChild(index > 0 ? droppable(line, 41 + index) : line);
      });
      body.appendChild(droppable(away, 40));
    }
    if (section.stale) {
      body.appendChild(droppable(
        el("div", "sub", "O último scan está velho: o scanner pode ter parado"), 60));
    }
    if (section.note) { body.appendChild(el("div", "sub", section.note)); }
  }

  function showBanner(text) {
    var banner = document.getElementById("banner");
    banner.hidden = !text;
    banner.textContent = text || "";
  }

  var lastStatus = null;

  function fitAll() {
    ["host", "containers", "backup", "network"].forEach(function (id) {
      fit(document.querySelector("#" + id + " .body"));
    });
  }

  function render(status) {
    lastStatus = status;
    var overall = document.getElementById("overall");
    overall.className = "overall s-" + status.overall;
    overall.textContent = OVERALL[status.overall] || "";
    document.getElementById("dot").className = "dot s-" + status.overall;
    renderHost(status.host, status.docker, status.updates);
    renderContainers(status.containers);
    renderBackup(status.backup);
    renderNetwork(status.network);
    fitAll();
    // Once more when the browser has settled the layout (fonts, scrollbars, a window being resized).
    setTimeout(fitAll, 150);
    var minutes = Math.round((status.age_seconds || 0) / 60);
    document.getElementById("updated").textContent =
      minutes < 1 ? "atualizado agora" : "atualizado há " + minutes + " min";
    document.body.className = status.stale ? "stale" : "";
    showBanner(status.stale ? "Os dados do servidor estão desatualizados" : "");
  }

  function load() {
    var request = new XMLHttpRequest();
    request.open("GET", "/api/status?t=" + Date.now());
    request.timeout = 10000;
    request.onload = function () {
      if (request.status === 200) {
        try {
          lastOk = Date.now();
          render(JSON.parse(request.responseText));
        } catch (e) {
          showBanner("Resposta inválida do servidor");
        }
      } else if (request.status === 503) {
        showBanner("O servidor está iniciando");
      } else {
        showBanner("Erro ao ler o servidor (" + request.status + ")");
      }
    };
    request.onerror = request.ontimeout = function () {
      showBanner("Sem ligação ao servidor");
      if (lastOk && Date.now() - lastOk > POLL_MS * 4) { document.body.className = "stale"; }
    };
    request.send();
  }

  function tick() {
    var now = new Date();
    document.getElementById("time").textContent =
      now.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  }

  function shift() {
    shiftIndex = (shiftIndex + 1) % SHIFTS.length;
    var root = document.documentElement.style;
    root.setProperty("--shift-x", SHIFTS[shiftIndex][0] + "px");
    root.setProperty("--shift-y", SHIFTS[shiftIndex][1] + "px");
  }

  var resizing = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizing);
    resizing = setTimeout(function () { if (lastStatus) { render(lastStatus); } }, 250);
  });

  tick();
  load();
  setInterval(tick, 10000);
  setInterval(load, POLL_MS);
  setInterval(shift, SHIFT_MS);
})();
