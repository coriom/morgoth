module.exports = {
  apps: [{
    name: "morgoth",
    script: "/home/corio/Morgoth/morgoth/.venv/bin/python",
    args: "main.py",
    cwd: "/home/corio/Morgoth/morgoth",
    interpreter: "none",
    autorestart: true,
    restart_delay: 5000,
    max_restarts: 10,
    min_uptime: 30000,
    log_file: "/home/corio/Morgoth/morgoth/data/logs/pm2-combined.log",
    out_file: "/home/corio/Morgoth/morgoth/data/logs/pm2-out.log",
    error_file: "/home/corio/Morgoth/morgoth/data/logs/pm2-error.log",
    log_date_format: "YYYY-MM-DD HH:mm:ss",
    watch: false,
    max_memory_restart: "2G",
    env: {
      PYTHONUNBUFFERED: "1"
    }
  }]
}
