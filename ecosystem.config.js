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
