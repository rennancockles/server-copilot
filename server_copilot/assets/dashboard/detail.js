(function () {
  "use strict";

  var POLL_MS = 15000;
  var PAGE = document.body.getAttribute("data-page");
  var PAGES = {
    containers: { api: "/api/containers", render: renderContainers },
    network: { api: "/api/network", render: renderNetwork }
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
  var PORTS = { 21: "FTP", 23: "Telnet", 2375: "API do Docker", 3389: "Área remota", 5900: "VNC" };
  var FINDINGS = {
    new_device: "Novo dispositivo",
    gateway_mac_changed: "MAC do roteador mudou",
    duplicate_mac: "MAC duplicado",
    risky_ports: "Portas de risco"
  };

  var STATES = {
    running: ["rodando", "ok"],
    exited: ["parado", "warn"],
    restarting: ["reiniciando", "bad"],
    paused: ["pausado", "warn"],
    dead: ["morto", "bad"],
    created: ["criado", "warn"]
  };
  var PROBLEMS = {
    unhealthy: "não saudável",
    restart_loop: "reiniciando em loop",
    oom_killed: "sem memória (OOM)",
    exited_with_error: "parou com erro",
    dead: "morto"
  };

  var lastOk = 0;

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
    if (gib >= 10) { return number(gib, 0) + " GB"; }
    if (gib >= 1) { return number(gib, 1) + " GB"; }
    var mib = value / 1048576;
    return mib >= 1 ? number(mib, 0) + " MB" : "< 1 MB";
  }

  function cell(text, cls) {
    return el("td", cls || "", text);
  }

  function head(labels) {
    var tr = el("tr");
    labels.forEach(function (label) {
      var numeric = label.charAt(0) === "#";
      tr.appendChild(el("th", numeric ? "num" : "", numeric ? label.slice(1) : label));
    });
    var thead = el("thead");
    thead.appendChild(tr);
    return thead;
  }

  function renderContainers(data) {
    var content = el("main");
    content.id = "content";
    if (data.state === "unavailable" || data.stacks.length === 0) {
      content.appendChild(el("div", "empty", data.note || "Nenhum container encontrado"));
      return content;
    }
    data.stacks.forEach(function (stack) {
      var name = stack.name || "sem stack";
      var card = el("section", "card s-" + stack.state);
      if (stack.name) { card.id = stack.name; }
      var title = el("h2", "", name);
      title.appendChild(el("span", "count",
        stack.running + " de " + stack.containers.length + " rodando"));
      card.appendChild(title);
      var table = el("table");
      table.appendChild(head(["Container", "Imagem", "Estado", "#CPU", "#Memória"]));
      var body = el("tbody");
      stack.containers.forEach(function (c) {
        var tr = el("tr");
        tr.appendChild(cell(c.service && c.service !== c.name ? c.service + " (" + c.name + ")" : c.name));
        tr.appendChild(cell(c.image, "mono sub"));
        var info = STATES[c.state] || [c.state, "warn"];
        var state = cell(info[0] + (c.problems.length ? " · " + c.problems.map(function (k) {
          return PROBLEMS[k] || k;
        }).join(", ") : ""), c.problems.length ? "bad" : info[1]);
        tr.appendChild(state);
        tr.appendChild(cell(c.cpu_percent === null ? "–" : number(c.cpu_percent, 1) + " %", "num"));
        tr.appendChild(cell(bytes(c.memory_usage_bytes), "num"));
        body.appendChild(tr);
      });
      table.appendChild(body);
      card.appendChild(table);
      content.appendChild(card);
    });
    return content;
  }

  function age(hours) {
    if (hours === null || hours === undefined) { return "sem registro"; }
    if (hours < 1) { return "há menos de 1 h"; }
    if (hours < 48) { return "há " + number(hours, 0) + " h"; }
    return "há " + number(hours / 24, 0) + " d";
  }

  function day(iso) {
    var date = new Date(iso);
    return isNaN(date.getTime()) ? "–" : date.toLocaleDateString("pt-BR");
  }

  function deviceName(d) {
    var parts = [d.name || d.vendor || "sem nome"];
    if (d.is_gateway) { parts.push("roteador"); }
    return parts.join(" · ");
  }

  function portsCell(d) {
    var td = cell("", "sub");
    if (d.open_ports.length === 0) { td.textContent = "–"; }
    d.open_ports.forEach(function (port, index) {
      var risky = d.risky_ports && d.risky_ports.indexOf(port) >= 0;
      var label = risky ? (PORTS[port] || "porta") + " " + port : String(port);
      var span = el("span", risky ? "warn" : "", label);
      if (index > 0) { td.appendChild(document.createTextNode(", ")); }
      td.appendChild(span);
    });
    return td;
  }

  function networkCard(title, count, state) {
    var card = el("section", "card s-" + state);
    var h2 = el("h2", "", title);
    h2.appendChild(el("span", "count", count));
    card.appendChild(h2);
    return card;
  }

  function renderNetwork(data) {
    var content = el("main");
    content.id = "content";
    if (data.state === "unavailable") {
      content.appendChild(el("div", "empty", data.note || "Sem dados da rede"));
      return content;
    }
    var notable = data.all_findings.filter(function (f) { return f.severity !== "info"; });
    if (notable.length > 0) {
      var box = networkCard("Avisos", String(notable.length), "ok");
      var list = el("table");
      var body = el("tbody");
      notable.forEach(function (f) {
        var tr = el("tr");
        tr.appendChild(cell(FINDINGS[f.kind] || f.kind, f.severity === "alert" ? "bad" : ""));
        tr.appendChild(cell(f.ip || "", "num"));
        tr.appendChild(cell(f.detail, "sub"));
        body.appendChild(tr);
      });
      list.appendChild(body);
      box.appendChild(list);
      content.appendChild(box);
    }

    var online = networkCard("Online", data.devices.length + " dispositivos", "ok");
    var table = el("table");
    table.appendChild(head(["Nome", "IP", "MAC", "Fabricante", "Tipo", "Portas abertas", "Visto desde"]));
    var rows = el("tbody");
    data.devices.forEach(function (d) {
      var tr = el("tr");
      tr.appendChild(cell(deviceName(d) + (d.is_new ? " · novo" : "")));
      tr.appendChild(cell(d.ip, "mono"));
      tr.appendChild(cell(d.mac || "–", "mono sub"));
      tr.appendChild(cell(d.vendor || "–", "sub"));
      tr.appendChild(cell(DEVICE_TYPES[d.device_type] || d.device_type, "sub"));
      tr.appendChild(portsCell(d));
      tr.appendChild(cell(day(d.first_seen), "sub"));
      rows.appendChild(tr);
    });
    table.appendChild(rows);
    online.appendChild(table);
    content.appendChild(online);

    if (data.offline_devices.length > 0) {
      var away = networkCard("Fora do ar", data.offline_devices.length + " dispositivos", "ok");
      var gone = el("table");
      gone.appendChild(head(["Nome", "IP", "MAC", "Fabricante", "Tipo", "Visto por último"]));
      var goneRows = el("tbody");
      data.offline_devices.forEach(function (d) {
        var tr = el("tr");
        tr.appendChild(cell(deviceName(d)));
        tr.appendChild(cell(d.ip || "–", "mono"));
        tr.appendChild(cell(d.mac || "–", "mono sub"));
        tr.appendChild(cell(d.vendor || "–", "sub"));
        tr.appendChild(cell(DEVICE_TYPES[d.device_type] || d.device_type, "sub"));
        tr.appendChild(cell(age(d.last_seen_age_hours), "sub"));
        goneRows.appendChild(tr);
      });
      gone.appendChild(goneRows);
      away.appendChild(gone);
      content.appendChild(away);
    }
    return content;
  }

  function showBanner(text) {
    var banner = document.getElementById("banner");
    banner.hidden = !text;
    banner.textContent = text || "";
  }

  function render(data) {
    var page = PAGES[PAGE];
    var next = page.render(data);
    var current = document.getElementById("content");
    current.parentNode.replaceChild(next, current);
    if (location.hash) {
      var target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
      if (target && !render.scrolled) {
        render.scrolled = true;
        target.scrollIntoView();
      }
    }
    var minutes = Math.round((data.age_seconds || 0) / 60);
    document.getElementById("updated").textContent =
      minutes < 1 ? "atualizado agora" : "atualizado há " + minutes + " min";
    document.body.className = data.stale ? "stale" : "";
    showBanner(data.stale ? "Os dados do servidor estão desatualizados" : "");
  }

  function load() {
    var request = new XMLHttpRequest();
    request.open("GET", PAGES[PAGE].api + "?t=" + Date.now());
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

  load();
  setInterval(load, POLL_MS);
})();
