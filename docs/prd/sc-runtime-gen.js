const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  HeadingLevel, AlignmentType, BorderStyle, WidthType, ShadingType,
  LevelFormat, PageBreak
} = require('docx');
const fs = require('fs');

const W = 9360;
const C = f => Math.round(W * f);
const b  = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
const hb = { style: BorderStyle.SINGLE, size: 1, color: "2E75B6" };
const bdr  = { top:b,  bottom:b,  left:b,  right:b  };
const hbdr = { top:hb, bottom:hb, left:hb, right:hb };
const cm = { top:80, bottom:80, left:120, right:120 };

const sp  = (before=120) => new Paragraph({ spacing:{before}, children:[] });
const rule = () => new Paragraph({
  spacing:{before:160,after:160},
  border:{bottom:{style:BorderStyle.SINGLE,size:4,color:"CCCCCC",space:1}},
  children:[]
});
const h1 = t => new Paragraph({
  heading:HeadingLevel.HEADING_1, spacing:{before:400,after:120},
  children:[new TextRun({text:t,bold:true,size:34,font:"Arial",color:"1F3864"})]
});
const h2 = t => new Paragraph({
  heading:HeadingLevel.HEADING_2, spacing:{before:260,after:80},
  children:[new TextRun({text:t,bold:true,size:26,font:"Arial",color:"2E75B6"})]
});
const h3 = t => new Paragraph({
  heading:HeadingLevel.HEADING_3, spacing:{before:180,after:60},
  children:[new TextRun({text:t,bold:true,size:24,font:"Arial",color:"404040"})]
});
const p = (text,opts={}) => new Paragraph({
  spacing:{before:60,after:60},
  children:[new TextRun({text,size:22,font:"Arial",...opts})]
});
const bl = (text,level=0) => new Paragraph({
  numbering:{reference:"bullets",level},
  spacing:{before:40,after:40},
  children:[new TextRun({text,size:22,font:"Arial"})]
});
const cd = text => new Paragraph({
  spacing:{before:20,after:20}, indent:{left:720},
  children:[new TextRun({text,size:20,font:"Courier New",color:"1A1A1A"})]
});

function hRow(cols,widths) {
  return new TableRow({ tableHeader:true, children: cols.map((c,i) =>
    new TableCell({ borders:hbdr, width:{size:widths[i],type:WidthType.DXA}, margins:cm,
      shading:{fill:"1F3864",type:ShadingType.CLEAR},
      children:[new Paragraph({children:[new TextRun({text:c,bold:true,size:20,font:"Arial",color:"FFFFFF"})]})]
    }))
  });
}
function dRow(cols,widths,shade=false) {
  return new TableRow({ children: cols.map((c,i) =>
    new TableCell({ borders:bdr, width:{size:widths[i],type:WidthType.DXA}, margins:cm,
      shading:{fill:shade?"EEF3FA":"FFFFFF",type:ShadingType.CLEAR},
      children:[new Paragraph({children:[new TextRun({text:c,size:20,font:"Arial"})]})]
    }))
  });
}
function tbl(headers,rows,widths) {
  return new Table({ width:{size:W,type:WidthType.DXA}, columnWidths:widths,
    rows:[hRow(headers,widths),...rows.map((r,i)=>dRow(r,widths,i%2===1))]
  });
}

function phaseHdr(label,title,desc) {
  return [
    sp(200),
    new Paragraph({
      spacing:{before:0,after:0},
      shading:{fill:"1F3864",type:ShadingType.CLEAR},
      border:{top:{style:BorderStyle.SINGLE,size:6,color:"1F3864"},
              bottom:{style:BorderStyle.SINGLE,size:6,color:"1F3864"}},
      children:[
        new TextRun({text:`${label}  `,bold:true,size:28,font:"Arial",color:"FFFFFF"}),
        new TextRun({text:title,bold:true,size:28,font:"Arial",color:"7FB3E8"}),
      ]
    }),
    new Paragraph({
      spacing:{before:60,after:80},
      children:[new TextRun({text:desc,size:21,font:"Arial",color:"444444",italics:true})]
    }),
  ];
}

function sprint(num,title,goals,tasks,deliverable) {
  const rows = [];
  rows.push(new TableRow({ tableHeader:true, children:[
    new TableCell({ borders:hbdr, columnSpan:2,
      width:{size:W,type:WidthType.DXA}, margins:cm,
      shading:{fill:"2E75B6",type:ShadingType.CLEAR},
      children:[new Paragraph({children:[
        new TextRun({text:`Sprint ${num}  `,bold:true,size:22,font:"Arial",color:"FFFFFF"}),
        new TextRun({text:title,size:22,font:"Arial",color:"BDD7EE"}),
      ]})]
    })
  ]}));
  const cell = (label,items,fill="EEF3FA",tfill="FFFFFF") => new TableRow({ children:[
    new TableCell({ borders:bdr, width:{size:C(0.18),type:WidthType.DXA}, margins:cm,
      shading:{fill,type:ShadingType.CLEAR},
      children:[new Paragraph({children:[new TextRun({text:label,bold:true,size:20,font:"Arial",color:"1F3864"})]})]
    }),
    new TableCell({ borders:bdr, width:{size:C(0.82),type:WidthType.DXA}, margins:cm,
      shading:{fill:tfill,type:ShadingType.CLEAR},
      children: items.map(g=>new Paragraph({
        numbering:{reference:"bullets",level:0}, spacing:{before:20,after:20},
        children:[new TextRun({text:g,size:20,font:"Arial"})]
      }))
    })
  ]});
  rows.push(cell("Goals",goals));
  rows.push(cell("Tasks",tasks));
  rows.push(new TableRow({ children:[
    new TableCell({ borders:bdr, width:{size:C(0.18),type:WidthType.DXA}, margins:cm,
      shading:{fill:"E8F0E8",type:ShadingType.CLEAR},
      children:[new Paragraph({children:[new TextRun({text:"Deliverable",bold:true,size:20,font:"Arial",color:"1F5C1F"})]})]
    }),
    new TableCell({ borders:bdr, width:{size:C(0.82),type:WidthType.DXA}, margins:cm,
      shading:{fill:"F5FFF5",type:ShadingType.CLEAR},
      children:[new Paragraph({children:[new TextRun({text:deliverable,size:20,font:"Arial",color:"1F5C1F"})]})]
    })
  ]}));
  return [new Table({width:{size:W,type:WidthType.DXA},columnWidths:[C(0.18),C(0.82)],rows}),sp(80)];
}

const children = [
  new Paragraph({spacing:{before:1440,after:200},
    children:[new TextRun({text:"sc-runtime",bold:true,size:64,font:"Arial",color:"1F3864"})]}),
  new Paragraph({spacing:{before:0,after:120},
    children:[new TextRun({text:"Product Requirements Document",size:30,font:"Arial",color:"2E75B6"})]}),
  new Paragraph({spacing:{before:0,after:600},
    children:[new TextRun({text:"Version 1.0  \u2022  June 2026  \u2022  Synaptic Canvas",size:21,font:"Arial",color:"888888"})]}),
  rule(), sp(400),

  h1("1. Overview"),
  p("sc-runtime is a foundational Rust crate that provides the four infrastructure primitives every Synaptic Canvas tool requires: a daemon lifecycle, a SQLite layer, a CLI scaffolding layer, and an RPC channel between CLI and daemon. It is extracted directly from atm-core, where these patterns have been designed and proven in production."),
  p("Without sc-runtime, each new tool (Continuity, ci, and every future tool) rebuilds these four primitives from scratch. The result is inconsistent implementations, wasted development time, and poor boundaries that prevent agents from following consistent patterns."),
  sp(),

  h2("1.1 Problem"),
  bl("Every SC tool needs daemon + SQLite + CLI + RPC \u2014 built from scratch each time"),
  bl("Agents lack good boundaries: each tool makes different infrastructure decisions"),
  bl("atm-core owns infrastructure it should not own, coupling ATM domain code to generic primitives"),
  bl("Continuity and ci cannot share infrastructure without depending on ATM internals"),
  sp(),

  h2("1.2 Solution"),
  bl("Extract proven infrastructure from atm-core into sc-runtime"),
  bl("All SC tools depend on sc-runtime for infrastructure, not on each other"),
  bl("atm-core migrates to depend on sc-runtime \u2014 reduces its own scope"),
  bl("Agents follow one consistent pattern across all tools"),
  sp(),

  h2("1.3 Goals"),
  bl("Single dependency for daemon + SQLite + CLI + RPC in any SC tool"),
  bl("Plugin trait as the standard unit of daemon extensibility"),
  bl("Unix socket RPC with automatic CLI-to-daemon routing"),
  bl("Zero ATM-specific concepts \u2014 sc-runtime has no knowledge of agents or messages"),
  bl("Cross-platform: macOS, Linux, Windows"),
  sp(),

  h2("1.4 Non-Goals"),
  bl("Not a framework \u2014 tools own their domain logic entirely"),
  bl("No agent messaging, no ATM protocol, no inbox/outbox concepts"),
  bl("No HTTP or network transport \u2014 Unix socket only in v1"),
  bl("No UI layer \u2014 sc-oneshot covers one-shot web prompts separately"),
  sp(),

  h1("2. Architecture"),

  h2("2.1 Position in the Ecosystem"),
  sp(40),
  cd("sc-runtime  (infrastructure: daemon, SQLite, CLI, RPC)"),
  cd("     \u251c\u2500\u2500 atm-core      (domain: agent messaging \u2014 migrates to consume sc-runtime)"),
  cd("     \u251c\u2500\u2500 Continuity    (domain: CI/PR monitoring)"),
  cd("     \u2514\u2500\u2500 ci            (domain: git/gh policy enforcement)"),
  sp(),
  p("sc-runtime has no dependencies on any SC domain crate. Dependency flows strictly downward."),
  sp(),

  h2("2.2 The Four Primitives"),
  sp(),
  tbl(
    ["Primitive","What It Provides","Extracted From"],
    [
      ["daemon","Singleton lifecycle: start, stop, restart, health. PID file. Signal handling (SIGTERM, SIGUSR1). Cancellation token propagation.","atm-daemon: event_loop, shutdown, pid_backend_validation"],
      ["db","SQLite open with WAL mode. Schema migration runner. Connection pool. ENV override for test isolation.","atm-core: schema/, retention, event_log patterns"],
      ["rpc","Unix domain socket server + client. Newline-delimited JSON protocol, versioned. Request/response with request_id. Graceful fallback when daemon not running.","atm-core: daemon_client, daemon_stream"],
      ["cli","Common subcommands: start, stop, restart, status, health. Clap integration. Automatic routing: CLI via socket if daemon running, direct if not.","atm-core: control, daemon_client auto-start pattern"],
    ],
    [C(0.15),C(0.52),C(0.33)]
  ),
  sp(),

  h2("2.3 Plugin Trait"),
  p("The Plugin trait is the standard unit of daemon extensibility, extracted directly from atm-daemon. Each domain feature implements Plugin. The daemon runner hosts any number of plugins."),
  sp(40),
  cd("pub trait Plugin: Send + Sync {"),
  cd("    fn metadata(&self) -> PluginMetadata;"),
  cd("    async fn init(&mut self, ctx: &PluginContext) -> Result<(), PluginError>;"),
  cd("    async fn run(&mut self, cancel: CancellationToken) -> Result<(), PluginError>;"),
  cd("    async fn shutdown(&mut self) -> Result<(), PluginError>;"),
  cd("}"),
  sp(),
  p("This is identical to the trait already proven in atm-daemon/src/plugin/traits.rs. Extraction is a rename and re-export, not a redesign."),
  sp(),

  h2("2.4 RPC Protocol"),
  p("Identical to the protocol proven in atm-core's daemon_client. Newline-delimited JSON over a Unix domain socket. One request line, one response line per connection. Versioned for forward compatibility."),
  sp(40),
  cd('// Request'),
  cd('{"version":1,"request_id":"uuid","command":"health","payload":{}}'),
  cd('// Response'),
  cd('{"version":1,"request_id":"uuid","status":"ok","payload":{"state":"running"}}'),
  sp(),
  p("Socket path: {SC_RUNTIME_HOME}/.sc/runtime/{tool-name}.sock"),
  p("SC_RUNTIME_HOME defaults to the OS home directory. Override for test isolation, matching the ATM_HOME pattern proven in atm-core."),
  sp(),

  h2("2.5 Consumer Pattern"),
  p("A tool using sc-runtime implements only its domain logic. Infrastructure is inherited:"),
  sp(40),
  cd("// continuity/src/main.rs"),
  cd("fn main() {"),
  cd("    ScRuntime::builder()"),
  cd("        .name(\"continuity\")"),
  cd("        .migrations(db::MIGRATIONS)"),
  cd("        .plugin(CiMonitorPlugin::new())"),
  cd("        .plugin(PrWatchPlugin::new())"),
  cd("        .cli(commands::register)"),
  cd("        .rpc(handlers::register)"),
  cd("        .build()"),
  cd("        .run();"),
  cd("}"),
  sp(),
  p("The CLI auto-routing means: if daemon is running, 'continuity status' routes via socket. If not, it executes directly. Consistent behavior either way, zero code in the consumer."),
  sp(),

  h1("3. Crate Structure"),
  sp(40),
  cd("sc-runtime/          (workspace root)"),
  cd("  Cargo.toml"),
  cd("  crates/"),
  cd("    sc-runtime-core/          \u2190 types, traits, protocol"),
  cd("      src/"),
  cd("        lib.rs"),
  cd("        plugin.rs             \u2190 Plugin trait, PluginMetadata, PluginContext"),
  cd("        pid.rs                \u2190 cross-platform process liveness (from atm-core verbatim)"),
  cd("        protocol.rs           \u2190 RPC request/response types"),
  cd("        home.rs               \u2190 SC_RUNTIME_HOME resolution"),
  cd("        error.rs              \u2190 ScRuntimeError"),
  cd(""),
  cd("    sc-runtime-db/            \u2190 SQLite layer"),
  cd("      src/"),
  cd("        lib.rs"),
  cd("        open.rs               \u2190 open with WAL, SC_RUNTIME_DB override"),
  cd("        migrate.rs            \u2190 migration runner"),
  cd("        pool.rs               \u2190 connection pool"),
  cd(""),
  cd("    sc-runtime-daemon/        \u2190 daemon lifecycle"),
  cd("      src/"),
  cd("        lib.rs"),
  cd("        lifecycle.rs          \u2190 start, stop, restart, health"),
  cd("        pid_file.rs           \u2190 write/read/clean PID file"),
  cd("        signals.rs            \u2190 SIGTERM, SIGUSR1 handlers"),
  cd("        registry.rs           \u2190 plugin registry, init/run/shutdown orchestration"),
  cd(""),
  cd("    sc-runtime-rpc/           \u2190 Unix socket RPC"),
  cd("      src/"),
  cd("        lib.rs"),
  cd("        server.rs             \u2190 socket listener, dispatch"),
  cd("        client.rs             \u2190 socket client, graceful fallback"),
  cd("        protocol.rs           \u2190 encode/decode newline-delimited JSON"),
  cd(""),
  cd("    sc-runtime-cli/           \u2190 CLI scaffolding"),
  cd("      src/"),
  cd("        lib.rs"),
  cd("        commands.rs           \u2190 start, stop, restart, status, health"),
  cd("        router.rs             \u2190 auto-route: socket if daemon running, direct if not"),
  cd(""),
  cd("    sc-runtime/               \u2190 facade crate, re-exports all above"),
  cd("      src/"),
  cd("        lib.rs                \u2190 ScRuntime builder, unified entry point"),
  sp(),

  h2("3.1 Key Dependencies"),
  tbl(
    ["Crate","Version","Use"],
    [
      ["tokio","1 (full)","Async runtime, cancellation tokens"],
      ["tokio-util","0.7","CancellationToken (matches atm-daemon usage)"],
      ["rusqlite","0.31 (bundled)","SQLite \u2014 bundled for single-binary consumers"],
      ["serde / serde_json","1","RPC protocol serialization"],
      ["anyhow","1","Error propagation"],
      ["libc","0.2","POSIX signal handling, pid liveness (Unix)"],
      ["clap","4 (derive)","CLI scaffolding"],
      ["uuid","1","request_id generation"],
    ],
    [C(0.20),C(0.15),C(0.65)]
  ),
  sp(),

  h1("4. Extraction Map \u2014 atm-core \u2192 sc-runtime"),
  p("This is not a redesign. Every item below is proven in production in atm-core or atm-daemon. The work is re-homing, renaming, and removing ATM-specific coupling."),
  sp(),
  tbl(
    ["Source (atm-core / atm-daemon)","Target (sc-runtime)","Notes"],
    [
      ["atm-core/src/pid.rs","sc-runtime-core/src/pid.rs","Zero changes. Cross-platform. No ATM coupling."],
      ["atm-core/src/home.rs","sc-runtime-core/src/home.rs","Rename ATM_HOME \u2192 SC_RUNTIME_HOME. Pattern identical."],
      ["atm-core/src/daemon_client.rs","sc-runtime-rpc/src/client.rs","Strip ATM-specific commands. Keep protocol, fallback, timeout."],
      ["atm-core/src/daemon_stream.rs","sc-runtime-rpc/src/protocol.rs","Keep newline-delimited JSON encode/decode. Remove ATM types."],
      ["atm-daemon/src/plugin/traits.rs","sc-runtime-core/src/plugin.rs","Remove InboxMessage dependency. Trait otherwise identical."],
      ["atm-daemon/src/plugin/registry.rs","sc-runtime-daemon/src/registry.rs","Remove ATM coupling. Keep init/run/shutdown orchestration."],
      ["atm-daemon/src/daemon/shutdown.rs","sc-runtime-daemon/src/lifecycle.rs","Keep cancellation token pattern. Remove ATM-specific state."],
      ["atm-daemon/src/daemon/socket.rs","sc-runtime-rpc/src/server.rs","Keep Unix socket listener. Remove ATM command routing."],
      ["atm-core/src/control.rs","sc-runtime-cli/src/commands.rs","Keep start/stop/restart/status. Remove ATM-specific commands."],
    ],
    [C(0.34),C(0.33),C(0.33)]
  ),
  sp(),

  h1("5. Requirements"),

  h2("5.1 Functional Requirements"),
  tbl(
    ["ID","Requirement"],
    [
      ["FR-01","Plugin trait provides init/run/shutdown lifecycle. Daemon hosts N plugins concurrently."],
      ["FR-02","Daemon enforces singleton via PID file. Second instance detects running daemon and routes to it."],
      ["FR-03","SIGTERM triggers graceful shutdown: cancellation token propagates to all plugins, shutdown() called in order."],
      ["FR-04","SIGUSR1 wakes daemon for immediate action. Plugins receive wake notification via cancellation/channel."],
      ["FR-05","SQLite opened with WAL mode. Schema migrations run on startup. SC_RUNTIME_DB overrides path."],
      ["FR-06","Unix socket at {SC_RUNTIME_HOME}/.sc/runtime/{tool}.sock. SC_RUNTIME_HOME overrides home dir."],
      ["FR-07","RPC protocol: newline-delimited JSON, version field, request_id for correlation."],
      ["FR-08","CLI client: if daemon running \u2192 route via socket. If not \u2192 execute directly. Transparent to caller."],
      ["FR-09","Common CLI commands: start (spawn daemon), stop (SIGTERM), restart, status, health."],
      ["FR-10","Graceful RPC fallback: connection refused or socket missing \u2192 Ok(None), not error."],
      ["FR-11","Plugin context provides: db connection, RPC dispatcher, sc-observability logger, cancel token."],
      ["FR-12","All primitives cross-platform: macOS, Linux, Windows. Unix socket unavailable on Windows \u2192 graceful fallback."],
    ],
    [C(0.10),C(0.90)]
  ),
  sp(),

  h2("5.2 Non-Functional Requirements"),
  tbl(
    ["ID","Requirement"],
    [
      ["NF-01","sc-runtime-core has zero SC domain dependencies. Importable by any Rust crate."],
      ["NF-02","sc-runtime does not know about ATM, agents, messages, CI, or git. Domain-free."],
      ["NF-03","rusqlite bundled feature. Consumers get SQLite with zero system dependency."],
      ["NF-04","Extraction from atm-core must not break atm-core. atm-core migrates to depend on sc-runtime as a follow-up."],
      ["NF-05","sc-observability wired by default in PluginContext. Tools get structured JSONL logging at no cost."],
    ],
    [C(0.10),C(0.90)]
  ),
  sp(),

  h1("6. Architecture Decision Records"),
  tbl(
    ["ADR","Decision","Rationale"],
    [
      ["ADR-01","Extract from atm-core; do not design from scratch","All four primitives are proven in production. Extraction is faster, lower risk, and validates the boundary cleanly."],
      ["ADR-02","Five sub-crates under sc-runtime workspace","Consumers take only what they need. A CLI-only tool may not need daemon. Fine-grained deps prevent bloat."],
      ["ADR-03","Unix socket only, no HTTP","Proven in atm-core. Simple, fast, no port management. Works on all Unix platforms. Windows falls back gracefully."],
      ["ADR-04","Newline-delimited JSON protocol","Identical to atm-core protocol. Debuggable with standard tools. No binary format complexity."],
      ["ADR-05","atm-core migrates to depend on sc-runtime","Validates the extraction. Reduces atm-core scope. Proves boundary is clean before other consumers build on it."],
      ["ADR-06","SC_RUNTIME_HOME mirrors ATM_HOME pattern","Proven override mechanism for test isolation. Consistent pattern across all SC tools."],
      ["ADR-07","sc-observability in PluginContext by default","Every tool gets structured JSONL logging without configuration. Consistent observability across the ecosystem."],
    ],
    [C(0.10),C(0.28),C(0.62)]
  ),
  sp(),

  new Paragraph({children:[new PageBreak()]}),
  h1("7. Development Plan"),
  p("Three phases. Phase A establishes the workspace and extracts core types. Phase B extracts and validates all four primitives. Phase C migrates atm-core and onboards first consumers. Each phase has a clear gate before the next begins."),
  sp(),
  tbl(
    ["Phase","Title","Sprints","Gate"],
    [
      ["A","Foundation","1\u20132","sc-runtime-core compiles. atm-core unchanged."],
      ["B","Primitive Extraction","3\u20136","All four primitives working. Integration tests pass."],
      ["C","Migration & Onboarding","7\u20139","atm-core migrated. Continuity and ci onboarded."],
    ],
    [C(0.10),C(0.22),C(0.12),C(0.56)]
  ),
  sp(200),

  ...phaseHdr("Phase A","Foundation","Workspace, core types, Plugin trait. Nothing extracted that touches atm-core yet. Establishes sc-runtime structure."),

  ...sprint(1,"Workspace & Core Types",
    [
      "sc-runtime workspace with six sub-crates created",
      "Plugin trait, PluginMetadata, PluginContext stubs defined",
      "pid.rs extracted from atm-core verbatim \u2014 zero changes",
      "home.rs extracted, ATM_HOME renamed SC_RUNTIME_HOME",
      "RPC request/response protocol types defined",
    ],
    [
      "Create Cargo.toml workspace root with resolver='2'",
      "Scaffold all six sub-crate directories with stub lib.rs files",
      "Copy atm-core/src/pid.rs to sc-runtime-core/src/pid.rs with no changes",
      "Copy atm-core/src/home.rs to sc-runtime-core/src/home.rs, rename env var only",
      "Define Plugin trait matching atm-daemon/src/plugin/traits.rs \u2014 remove InboxMessage",
      "Define RpcRequest / RpcResponse structs with version + request_id fields",
      "Unit tests: pid liveness, home resolution with SC_RUNTIME_HOME env var override",
      "Verify atm-core still builds with zero changes",
    ],
    "sc-runtime workspace builds. Core types and Plugin trait defined. pid and home extracted and tested."
  ),

  ...sprint(2,"Audit & Facade",
    [
      "Full audit of atm-core and atm-daemon: every module mapped to extraction target",
      "sc-runtime facade crate re-exports all sub-crates via single entry point",
      "CI check: sc-runtime-core must have zero SC domain crate dependencies",
    ],
    [
      "Read every src/*.rs in atm-core and atm-daemon/src/",
      "Produce extraction map: source module \u2192 target module, ATM coupling to remove",
      "Define feature flags for optional sub-crates (db, rpc, cli, daemon)",
      "Write sc-runtime/src/lib.rs facade with conditional re-exports",
      "Add CI check: sc-runtime-core dependency tree contains no SC domain crates",
      "Write ScRuntime builder stub \u2014 compiles but not yet functional",
    ],
    "Complete extraction map documented. Facade crate compiles. Dependency audit CI check passes."
  ),

  ...phaseHdr("Phase B","Primitive Extraction","Extract and validate all four primitives from atm-core and atm-daemon. One primitive per sprint, tested in isolation before the next begins."),

  ...sprint(3,"SQLite Layer (sc-runtime-db)",
    [
      "sc-runtime-db: open with WAL, SC_RUNTIME_DB override, migration runner",
      "Connection pool with configurable size",
      "Test isolation via SC_RUNTIME_DB env var proven",
    ],
    [
      "Extract WAL open pattern from atm-core schema patterns",
      "Implement migrate.rs: ordered migration runner from &[(&str, &str)] (version, sql)",
      "Implement pool.rs: connection pool (r2d2-sqlite or equivalent)",
      "SC_RUNTIME_DB env var overrides default path for test isolation",
      "Integration test: migrations run on fresh db, idempotent on re-run",
      "Integration test: two consumers open same db concurrently \u2014 WAL allows both",
    ],
    "sc-runtime-db fully functional. WAL, migrations, pool, and test isolation all tested."
  ),

  ...sprint(4,"RPC Layer (sc-runtime-rpc)",
    [
      "Unix socket server + client extracted from atm-core",
      "Newline-delimited JSON protocol, versioned, request_id correlation",
      "Graceful client fallback: Ok(None) when daemon not running",
    ],
    [
      "Extract socket server from atm-daemon/src/daemon/socket.rs \u2014 remove ATM command routing",
      "Extract client from atm-core/src/daemon_client.rs \u2014 keep protocol, timeout, fallback",
      "Extract encode/decode from atm-core/src/daemon_stream.rs",
      "Socket path: {SC_RUNTIME_HOME}/.sc/runtime/{tool}.sock",
      "Integration test: server starts, client connects, round-trip request/response",
      "Integration test: client returns Ok(None) when no server running",
      "Integration test: concurrent requests via request_id correlation",
    ],
    "sc-runtime-rpc fully functional. Server/client round-trip tested. Graceful fallback tested."
  ),

  ...sprint(5,"Daemon Lifecycle (sc-runtime-daemon)",
    [
      "Plugin registry: init/run/shutdown orchestration with cancellation token",
      "PID file: write on start, read for singleton check, delete on clean exit",
      "Signal handling: SIGTERM graceful shutdown, SIGUSR1 plugin wake",
    ],
    [
      "Extract plugin registry from atm-daemon/src/plugin/registry.rs \u2014 remove ATM coupling",
      "Extract shutdown from atm-daemon/src/daemon/shutdown.rs \u2014 keep cancellation pattern",
      "Implement pid_file.rs: write, read, stale detection via sc-runtime-core pid liveness",
      "Implement signals.rs: SIGTERM \u2192 cancel token, SIGUSR1 \u2192 wake channel",
      "Integration test: start daemon, SIGTERM received, all plugins shutdown() called",
      "Integration test: stale PID file from dead process detected and cleaned",
      "Integration test: SIGUSR1 wakes daemon without terminating it",
    ],
    "Daemon lifecycle fully functional. Plugin orchestration, PID singleton, and signals all tested."
  ),

  ...sprint(6,"CLI Scaffolding & ScRuntime Builder (sc-runtime-cli)",
    [
      "Common commands: start, stop, restart, status, health",
      "Auto-routing: socket if daemon running, direct execution if not",
      "ScRuntime builder wires all four primitives into a single entry point",
      "End-to-end: toy plugin runs via ScRuntime builder",
    ],
    [
      "Extract control commands from atm-core/src/control.rs \u2014 remove ATM-specific commands",
      "Implement router.rs: check socket \u2192 route or execute direct",
      "Complete ScRuntime builder in sc-runtime/src/lib.rs",
      "Integration test: start daemon via builder, verify status routes via socket",
      "Integration test: stop daemon, verify status executes directly without socket",
      "End-to-end test: toy plugin (NoopPlugin) implements Plugin, runs via ScRuntime",
      "Verify sc-observability wired in PluginContext with no consumer config required",
    ],
    "All four primitives integrated. ScRuntime builder produces a working daemon+CLI from a toy plugin."
  ),

  ...phaseHdr("Phase C","Migration & Onboarding","atm-core migrates to sc-runtime. Continuity and ci onboard as first real consumers. Validates the boundary is clean."),

  ...sprint(7,"atm-core Migration",
    [
      "atm-core depends on sc-runtime instead of owning infrastructure",
      "ATM-specific modules replaced with sc-runtime imports",
      "All atm-core and atm-daemon tests pass. No behavior change.",
    ],
    [
      "Replace atm-core/src/pid.rs with sc_runtime_core::pid re-export",
      "Replace atm-core/src/home.rs pattern with sc_runtime_core::home",
      "Replace atm-core/src/daemon_client.rs with sc_runtime_rpc::client",
      "Replace atm-core/src/daemon_stream.rs with sc_runtime_rpc::protocol",
      "Replace atm-daemon plugin registry with sc_runtime_daemon::registry",
      "Run full atm test suite \u2014 all tests must pass",
      "Verify atm binary behavior identical before and after migration",
    ],
    "atm-core migrated. No behavior change. Scope of atm-core reduced. sc-runtime boundary validated in production."
  ),

  ...sprint(8,"Continuity Onboarding",
    [
      "Continuity daemon implemented as sc-runtime Plugins",
      "Continuity SQLite via sc-runtime-db",
      "Continuity CLI commands via sc-runtime-cli auto-routing",
    ],
    [
      "Replace Continuity daemon infrastructure with ScRuntime builder",
      "Migrate Continuity SQLite open/migrate to sc-runtime-db",
      "Register Continuity CLI commands as sc-runtime-cli extensions",
      "CiMonitorPlugin and PrWatchPlugin implement Plugin trait",
      "Verify all Continuity integration tests pass against new infrastructure",
      "Verify sc-observability still wired via PluginContext",
    ],
    "Continuity runs on sc-runtime. Infrastructure code eliminated from Continuity domain crates."
  ),

  ...sprint(9,"ci Onboarding & Documentation",
    [
      "ci tool implemented on sc-runtime",
      "sc-runtime README and consumer guide written",
      "Migration guide for future SC tools",
    ],
    [
      "ci SQLite (rules, state, account paths) via sc-runtime-db",
      "ci CLI passthrough and policy commands via sc-runtime-cli",
      "ci daemon (rules watcher, if needed) via sc-runtime-daemon",
      "Write README: overview, consumer pattern, Plugin example, env vars",
      "Write MIGRATION.md: how to migrate an existing tool to sc-runtime",
      "Verify ci integration tests pass",
    ],
    "ci runs on sc-runtime. Documentation complete. sc-runtime ready for any future SC tool."
  ),

  sp(200), rule(),
  new Paragraph({spacing:{before:80},
    children:[new TextRun({text:"sc-runtime  \u2022  v1.0  \u2022  June 2026  \u2022  Synaptic Canvas",size:18,font:"Arial",color:"888888"})]
  }),
];

const doc = new Document({
  numbering:{config:[{reference:"bullets",levels:[
    {level:0,format:LevelFormat.BULLET,text:"\u2022",alignment:AlignmentType.LEFT,
      style:{paragraph:{indent:{left:720,hanging:360}}}},
    {level:1,format:LevelFormat.BULLET,text:"\u25e6",alignment:AlignmentType.LEFT,
      style:{paragraph:{indent:{left:1080,hanging:360}}}},
  ]}]},
  styles:{
    default:{document:{run:{font:"Arial",size:22}}},
    paragraphStyles:[
      {id:"Heading1",name:"Heading 1",basedOn:"Normal",next:"Normal",quickFormat:true,
        run:{size:34,bold:true,font:"Arial",color:"1F3864"},
        paragraph:{spacing:{before:400,after:120},outlineLevel:0}},
      {id:"Heading2",name:"Heading 2",basedOn:"Normal",next:"Normal",quickFormat:true,
        run:{size:26,bold:true,font:"Arial",color:"2E75B6"},
        paragraph:{spacing:{before:260,after:80},outlineLevel:1}},
      {id:"Heading3",name:"Heading 3",basedOn:"Normal",next:"Normal",quickFormat:true,
        run:{size:24,bold:true,font:"Arial",color:"404040"},
        paragraph:{spacing:{before:180,after:60},outlineLevel:2}},
    ]
  },
  sections:[{
    properties:{page:{
      size:{width:12240,height:15840},
      margin:{top:1440,right:1440,bottom:1440,left:1440}
    }},
    children
  }]
});

const OUT = '/Volumes/Extreme Pro/github/continuity/docs/prd/sc-runtime-prd.docx';
Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync(OUT, buf);
  console.log('Written to: ' + OUT);
}).catch(e => { console.error(e); process.exit(1); });
