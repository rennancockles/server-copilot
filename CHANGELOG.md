# CHANGELOG

<!-- version list -->

## v0.11.0 (2026-10-28)

### Features

- Add Compose update tools that queue requests for a host runner
  ([`92f7f72`](https://github.com/rennancockles/server-copilot/commit/92f7f728f11e38604dff572b2a7f3604b6141d40))


## v0.10.0 (2026-10-28)

### Chores

- Update .env.example with the scanner, backup status and readable dirs variables
  ([`12ac048`](https://github.com/rennancockles/server-copilot/commit/12ac048656349a902ca3b2e084ef878ee598dffc))

### Features

- Add read_text_file MCP tool for readable directories
  ([`0ca121b`](https://github.com/rennancockles/server-copilot/commit/0ca121ba85277a27846924dd799ea7ba1bcab72d))


## v0.9.0 (2026-10-27)

### Features

- Add LAN device discovery MCP tools and scanner service
  ([`d8adcbc`](https://github.com/rennancockles/server-copilot/commit/d8adcbc92fd1da5b39c5240bba2b61230b6dc022))


## v0.8.0 (2026-10-27)

### Features

- Add get_backup_status MCP tool
  ([`155802b`](https://github.com/rennancockles/server-copilot/commit/155802b1eb068b17c7f8990ace11af4651da284c))

### Testing

- Freeze the clock without subclassing datetime
  ([`208292d`](https://github.com/rennancockles/server-copilot/commit/208292d0157a750da001d407dc611f185ce00605))


## v0.7.0 (2026-10-22)

### Features

- Add get_compose_projects MCP tool
  ([`9c2932f`](https://github.com/rennancockles/server-copilot/commit/9c2932f4b0df5e1c7cc26554dc715cbdc99672b5))

- Add get_docker_disk_usage MCP tool
  ([`6775a1c`](https://github.com/rennancockles/server-copilot/commit/6775a1c9f85d4caeaa3d63931b1e22d626ee6182))


## v0.6.0 (2026-10-22)

### Bug Fixes

- Separar o log de 403 do de 401 e registrar o IP real do cliente atrás do túnel (146)
  ([`1b71fc0`](https://github.com/rennancockles/server-copilot/commit/1b71fc081633f72654de956f0cf5d1c3fd48f118))

### Documentation

- Apontar para o runbook de operação do OAuth (148)
  ([`7b66fa3`](https://github.com/rennancockles/server-copilot/commit/7b66fa3f5ff28ad99cda2dfdb6e35dafaedb3c36))

### Features

- Versionar o plugin do ChatGPT com o servidor e anexar o zip a cada release
  ([`72c16dc`](https://github.com/rennancockles/server-copilot/commit/72c16dcc52a9691d021d9cba699238addc69ca2e))


## v0.5.0 (2026-10-22)

### Features

- Logar de onde vem o email do token OAuth e tornar os logs de OAuth visíveis (146)
  ([`085d845`](https://github.com/rennancockles/server-copilot/commit/085d8451d11e0889da4e8642e4c6621ee721a5db))


## v0.4.0 (2026-10-22)

### Features

- Autenticar o /mcp com OAuth (WorkOS AuthKit) e remover a URL com token (144)
  ([`6b2fab8`](https://github.com/rennancockles/server-copilot/commit/6b2fab829e3ba464b7f052fd5ab5409eb6f0b4ef))

### Testing

- Isolate the suite from SERVER_COPILOT_* env vars and the local .env
  ([`af49f07`](https://github.com/rennancockles/server-copilot/commit/af49f07ce5326751e72f9afcb230cfe2ce8c85d6))


## v0.3.0 (2026-10-20)

### Features

- Add get_disk_usage MCP tool
  ([`57f11fa`](https://github.com/rennancockles/server-copilot/commit/57f11fa8ddc40822ba96284085a60bd05b9e8022))

- Add list_files MCP tool
  ([`4cbae95`](https://github.com/rennancockles/server-copilot/commit/4cbae95ea36434a95f1442d4564073b0232512e7))

- Hide dotfiles from get_disk_usage unless include_hidden is set
  ([`cd3edbd`](https://github.com/rennancockles/server-copilot/commit/cd3edbd100c5c4d80b08667cecb178a6d08d666c))

### Testing

- List every entry in the real du test so the symlink is found
  ([`8f9b526`](https://github.com/rennancockles/server-copilot/commit/8f9b526a8d96c216347fd83abafa0779f737570c))


## v0.2.0 (2026-10-20)

### Chores

- Rename the docker network to docker-api and add an edge network
  ([`66fb0c4`](https://github.com/rennancockles/server-copilot/commit/66fb0c46d1ed00a22ef14dcebe7f0422158853ac))

### Features

- Serve the MCP endpoint at a secret URL for ChatGPT connectors
  ([`a2f26d4`](https://github.com/rennancockles/server-copilot/commit/a2f26d45b66fb77f245460a46137e63dfaa78454))


## v0.1.0 (2026-10-19)

- Initial Release
