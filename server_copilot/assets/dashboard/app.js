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
    return number(gib, 1) + " GB";
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

  function renderHost(section) {
    var body = panel("host", section);
    if (section.cpu_percent === null) { return unavailable(body, section); }
    var meters = el("div", "meters");
    meters.appendChild(meter("CPU", section.cpu_percent, number(section.cpu_percent, 0) + " %",
      level(section.cpu_percent, 85, 97)));
    meters.appendChild(meter("Memória", section.memory_percent,
      bytes(section.memory_used_bytes) + " de " + bytes(section.memory_total_bytes),
      level(section.memory_percent, 90, 97)));
    section.disks.forEach(function (disk) {
      meters.appendChild(meter("Disco " + disk.name, disk.percent,
        number(disk.percent, 0) + " % de " + bytes(disk.total_bytes),
        level(disk.percent, 85, 95)));
    });
    body.appendChild(meters);
    body.appendChild(row("Carga (1, 5, 15 min)",
      section.load_average.map(function (n) { return number(n, 2); }).join("   ")));
    body.appendChild(row("Ligado há", duration(section.uptime_seconds)));
    if (section.note) { body.appendChild(el("div", "sub", section.note)); }
  }

  function consumers(title, rows, valueOf) {
    var column = el("div");
    column.appendChild(el("h3", "", title));
    var list = el("div", "list");
    rows.forEach(function (r) { list.appendChild(item(r.name, valueOf(r))); });
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
      section.problems.slice(0, 4).forEach(function (p) {
        var kinds = p.problems.map(function (k) { return PROBLEMS[k] || k; }).join(", ");
        list.appendChild(item(p.name, kinds, "bad"));
      });
      if (section.problems.length > 4) {
        list.appendChild(item("+" + (section.problems.length - 4) + " outros", "", "bad"));
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
    body.appendChild(row("Último backup", age(section.last_success_age_hours)));
    section.repositories.forEach(function (repo) {
      var text = age(repo.snapshot_age_hours);
      if (repo.free_bytes !== null && repo.free_bytes !== undefined) {
        text += "  ·  " + bytes(repo.free_bytes) + " livres";
      }
      body.appendChild(row("Cópia " + repo.name, text));
    });
    body.appendChild(row("Verificação semanal", CHECKS[section.integrity] || "sem registro"));
    var restore = CHECKS[section.restore_test] || "sem registro";
    if (section.restore_next_run_at) {
      var next = new Date(section.restore_next_run_at);
      if (!isNaN(next.getTime())) {
        restore += "  ·  próximo " + next.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" });
      }
    }
    body.appendChild(row("Teste de restauração", restore));
    if (section.state !== "ok" && section.summary) { body.appendChild(el("div", "sub", section.summary)); }
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
    body.appendChild(chips);
    if (section.findings.length === 0) {
      body.appendChild(el("div", "sub", "Nada incomum na rede"));
    } else {
      var list = el("div", "list");
      section.findings.slice(0, 3).forEach(function (f) {
        list.appendChild(item(FINDINGS[f.kind] || f.kind, f.ip || "", f.severity === "alert" ? "bad" : "warn"));
      });
      body.appendChild(list);
    }
    if (section.note) { body.appendChild(el("div", "sub", section.note)); }
  }

  function showBanner(text) {
    var banner = document.getElementById("banner");
    banner.hidden = !text;
    banner.textContent = text || "";
  }

  function render(status) {
    var overall = document.getElementById("overall");
    overall.className = "overall s-" + status.overall;
    overall.textContent = OVERALL[status.overall] || "";
    document.getElementById("dot").className = "dot s-" + status.overall;
    renderHost(status.host);
    renderContainers(status.containers);
    renderBackup(status.backup);
    renderNetwork(status.network);
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

  tick();
  load();
  setInterval(tick, 10000);
  setInterval(load, POLL_MS);
  setInterval(shift, SHIFT_MS);
})();
