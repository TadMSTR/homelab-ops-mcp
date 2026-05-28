module.exports = {
  apps: [
    {
      name: "system-ops",
      script: "/home/ted/repos/personal/homelab-ops-mcp/.venv/bin/python3",
      args: ["server.py", "--host", "127.0.0.1", "--port", "8282"],
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
