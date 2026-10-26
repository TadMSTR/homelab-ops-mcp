module.exports = {
  apps: [
    {
      name: "system-ops",
      // run-hardened.sh sets `ulimit -c 0` before exec'ing the venv python3 server —
      // PM2 fork_mode has no native ulimit option (HLOPS-1 audit LOW: no core dumps on crash).
      script: "/home/ted/repos/personal/homelab-ops-mcp/run-hardened.sh",
      args: ["--host", "127.0.0.1", "--port", "8282"],
      cwd: "/home/ted/repos/personal/homelab-ops-mcp",
      interpreter: "none",

      // Declared explicitly, and deliberately minimal.
      //
      // Every variable the server reads has a safe in-code default, so only the
      // ones set differently here are declared. The full list is in the README:
      // LOG_LEVEL, LOG_FILE, SYSTEM_OPS_CHILD_ENV_ENFORCE,
      // SYSTEM_OPS_CHILD_ENV_ALLOWLIST, SYSTEM_OPS_OUTPUT_LIMIT_BYTES,
      // SYSTEM_OPS_ALLOWED_HOSTS and the telemetry endpoints. Host and port are
      // passed as `args` above, not through the environment. With the default
      // loopback bind, SYSTEM_OPS_ALLOWED_HOSTS stays unset: loopback Host names
      // are accepted on any port without it.
      //
      // SYSTEM_OPS_CHILD_ENV_ENFORCE is pinned to "false" rather than left
      // unset even though false is the in-code default. `pm2 restart
      // --update-env` can change a variable but cannot delete one, so a
      // variable that is present from the start can be flipped back; one that
      // was never declared can only be removed by `pm2 delete` + `pm2 start`
      // from a shell that does not have it set. Declaring it now is what makes
      // the rollback a one-line edit later.
      //
      // A process started by hand from an interactive shell carries far more
      // environment than this block declares, because PM2 freezes the whole
      // shell environment into its dump. That is not configuration, and
      // reproducing it here would be harmful: it would put whatever that shell
      // had sourced into a process that does not read it, and PM2 would write
      // it straight back out at the next `pm2 save`.
      //
      // Note this does not scrub inheritance: PM2 always passes the parent
      // environment through, so a `pm2 start` from an interactive shell still
      // inherits whatever that shell sourced. What this block asserts is what
      // the app *requires*.
      env: {
        SYSTEM_OPS_CHILD_ENV_ENFORCE: "false",
        // Set explicitly rather than relying on PM2 capturing stderr. With it
        // set, the server writes its own JSON stream to this file and stderr
        // carries nothing, so there is one writer and one format.
        //
        // Deliberately the same path as out_file/error_file below: the file is
        // already covered by /etc/logrotate.d/forge-logs
        // (/home/ted/logs/[!b]*.log, daily, rotate 14, copytruncate), and a
        // second path would need a second logrotate claim. The handle is opened
        // in append mode, which is what makes copytruncate safe — writes resume
        // at the new end of file rather than leaving a sparse gap.
        //
        // Do NOT add pm2-logrotate. It is not installed, and a second mechanism
        // would double-claim these files, which logrotate treats as a hard
        // error that fails the whole daily run.
        LOG_FILE: "/home/ted/logs/system-ops.log",

        // Telemetry. Only OTLP is enabled; the InfluxDB and NATS sinks stay
        // unset, so telemetry.py never starts its background loop thread.
        //
        // 127.0.0.1:4317 is the SigNoz collector's gRPC port. Taken from
        // dockhand-mcp, another host-side PM2 MCP server, which uses this exact
        // endpoint and does appear in SigNoz — so it is proven from this
        // deployment shape rather than merely reachable.
        //
        // Not INFLUXDB_URL from forge.env: that name holds a container-network
        // address host processes cannot resolve, and port 8181 on this host is
        // qmd. See vikunja#575.
        OTEL_EXPORTER_OTLP_ENDPOINT: "http://127.0.0.1:4317",
        // The PM2 app name, so the service is findable in SigNoz under the name
        // an operator already uses for it. The package default is
        // "homelab-ops-mcp".
        OTEL_SERVICE_NAME: "system-ops",
      },

      restart_delay: 5000,
      max_restarts: 10,
      min_uptime: "10s",

      out_file: "/home/ted/logs/system-ops.log",
      error_file: "/home/ted/logs/system-ops.log",
      merge_logs: true,
      time: true,
    },
  ],
};
