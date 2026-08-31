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

      // Empty ON PURPOSE — this server requires nothing from the environment.
      // The only two variables the source reads are LOG_LEVEL and LOG_FILE
      // (src/homelab_ops_mcp/logging.py). Both are unset in the running
      // process and both have safe in-code defaults: INFO, and stderr. Host
      // and port are passed explicitly as `args` above.
      //
      // This one is worth spelling out because the running process carries far
      // more environment than any other app declared in this repo — a large
      // inherited bundle plus a stray API token, none of which it reads. That
      // is a side effect of having been started by hand from an interactive
      // shell, which freezes the whole shell environment into PM2's dump. It
      // is not configuration, and reproducing it would be actively harmful:
      // it would put credentials into a process that ignores them, and PM2
      // would write them straight back out at the next `pm2 save`.
      //
      // Stated explicitly rather than omitted so that "no env block" can no
      // longer be read two ways. A declaration silent about env is
      // indistinguishable from one where the env was lost, and that ambiguity
      // is what made this app unsafe to `pm2 delete` and re-create even though
      // it was already declared here.
      //
      // Note this does not scrub inheritance: PM2 always passes the parent
      // environment through, so a `pm2 start` from an interactive shell still
      // inherits whatever that shell sourced. What this block asserts is what
      // the app *requires*, which is nothing.
      env: {},

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
