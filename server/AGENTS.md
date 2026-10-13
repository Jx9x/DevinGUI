# Backend code conventions

- Keep FastAPI app wiring in `main.py` and route orchestration separate from
  session ownership. Registry operations belong in `session_manager.py` and
  ACP protocol I/O in `acp_client.py`.
- Keep ACP update translation, tool-content formatting, and permission selection
  in the pure `acp_updates.py` module. Keep live ACP activity, raw tool snapshots,
  attached-socket delivery, and buffer/replay policy in `session_notifications.py`.
  State remains on `ManagedSession`; callbacks resolve the current session across
  handoffs, while replay uses the supplied session snapshot. Preserve `main.py`
  import aliases for existing consumers.
- Use explicit, typed helpers for repeated lifecycle operations. Keep transition
  locks and generation checks visible in the owning operations; keep generation
  advances and turn/transition task scheduling visible in command dispatch.
  Keep browser prompt preparation and cancellation in `TurnCommands`, using the
  receive loop's session snapshot and direct socket reply. Return a prepared
  prompt before advancing generations; watcher/pricing setup and turn creation
  remain in dispatch. Resolve shared preparation services through typed getters.
- Cancel and await active turn/transition tasks with `cancel_task`, preserving the
  caller's logger and task description. Clearing task references belongs to the
  owning operation's existing generation/identity guards.
- Keep turn completion races, live watcher callbacks, and final persistence
  snapshots in `TurnCoordinator`. Keep fallback and task ownership state on
  `ManagedSession`, and resolve the current session/socket through endpoint
  callbacks so surviving turns follow reconnects.
- Keep session startup, history selection, and live reconnect handoffs in
  `SessionTransitionCoordinator`. Construct ACP through the endpoint's factory
  for notification/permission wiring, and resolve shared services through typed
  getters. Preserve distinct view/load/resume frame policies and silent compaction
  restarts; task/process ownership remains on `ManagedSession`.
- Keep ACP permission requests, timeout scheduling, and browser decisions in
  `PermissionCoordinator`; pending requests and tasks remain on `ManagedSession`
  for teardown and reconnects. ACP events use current-session/socket callbacks;
  browser decisions use the receive loop's session snapshot and direct socket
  reply. Preserve those delivery/error boundaries and distinct correlation IDs.
- Keep REST handlers and request models in feature router modules. Construct
  shared services in `main.py` and pass typed callbacks that resolve them at
  request time. Preserve route signatures, execution modes, and registration
  before static frontend routes; share structured errors through `rest_errors.py`.
- Keep protobuf decoding and model metadata conversion in pure `model_catalog.py`.
  Cache discovery, envelope I/O, decoder fallback, and bounded memoization belong
  in `model_cache.py`. Keep catalogue precedence, availability, and saved-choice
  migration in `model_availability.py`, with explicit metadata/state inputs and
  the caller's logger. Preserve staged updates on failures. Cache selection,
  registry persistence, DB discovery, and CLI process ownership stay in
  `model_registry.py`; preserve its used parser/directory hooks.
- Keep stored message/reasoning projection in pure `message_projection.py`,
  retaining `DBReader` conversion hooks and recursive text dispatch. SQL selection,
  ordering, main-chain traversal, and connections belong in `db_reader.py`.
  Keep shared cost assembly and reasoning merging in `session_history.py`; pass
  cost services explicitly and retain `main.py` hooks that resolve current services
  for REST and WebSocket consumers. Preserve historical pricing, source order,
  and branch/sample precedence.
- Access the session registry through `SessionManager` methods. Removal must check
  both the registry key and the instance so stale cleanup cannot evict a replacement.
- Preserve the order of permission rejection, ACP shutdown, watcher cleanup, and
  registry changes. Transition cleanup and orphan cleanup have different orders.
- Keep ACP model, effort, speed, and mode selection/verification in
  `acp_configuration.py`, using its typed option-request interface. Request I/O
  and option-cache updates remain in `acp_client.py`; shared exceptions belong in
  `acp_errors.py`, retaining client imports. Verify the selected model before
  registering a ready session. Bypass mode remains best-effort, and history replay
  suppression must reset in `finally`.
- Treat wire keys, response shapes, error messages, logging, timeout values, and
  reconnect behavior as compatibility contracts during refactors.
- Keep backend tests in `server/test/` as `*_test.py`, using pytest and the
  existing fake ACP/WebSocket objects. Use mocks and temporary data instead of
  accessing real profiles, credentials, conversations, or live services.
- Keep per-user CLI/GUI path resolution in `paths.py`; retain imported helper
  aliases used by consumers and tests. ACP and model discovery must share the
  `DEVINGUI_DEVIN_BIN` resolver. Preserve existing archive roots/indexes and
  read-only database access; only missing databases produce empty listing results.
