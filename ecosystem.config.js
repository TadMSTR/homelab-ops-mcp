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
      // The server reads four variables, all with safe in-code defaults:
      // LOG_LEVEL (INFO), LOG_FILE (stderr), SYSTEM_OPS_CHILD_ENV_ENFORCE
      // (false) and SYSTEM_OPS_CHILD_ENV_ALLOWLIST (empty). Host and port are
      // passed as `args` above, not through the environment.
      //
      // SYSTEM_OPS_CHILD_ENV_ENFORCE is pinned to "false" rather than left
      // unset even though false is the in-code default. `pm2 restart
      // --update-env` can change a variable but cannot delete one, so a
      // variable that is present from the start can be flipped back; one that
      // was never declared can only be removed by `pm2 delete` + `pm2 start`
      // from a shell that does not have it set. Declaring it now is what makes
      // the rollback a one-line edit later.
      //
      // Worth spelling out because the running process carries far more
      // environment than this block declares — a large inherited bundle plus a
      // stray API token, none of which the server reads. That is a side effect
      // of having been started by hand from an interactive shell, which freezes
      // the whole shell environment into PM2's dump. It is not configuration,
      // and reproducing it would be actively harmful: it would put credentials
      // into a process that ignores them, and PM2 would write them straight
      // back out at the next `pm2 save`.
      //
      // Note this does not scrub inheritance: PM2 always passes the parent
      // environment through, so a `pm2 start` from an interactive shell still
      // inherits whatever that shell sourced. What this block asserts is what
      // the app *requires*.
      env: {
        SYSTEM_OPS_CHILD_ENV_ENFORCE: "false",
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
