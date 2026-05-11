#!/bin/bash
set -e
cd /home/corio/Morgoth/morgoth

mkdir -p data/logs

if ! command -v pm2 &> /dev/null; then
  echo "Installing pm2..."
  npm install -g pm2
fi

pm2 delete morgoth 2>/dev/null || true
pm2 start ecosystem.config.js
pm2 save
pm2 logs morgoth --lines 50 &
sleep 60
kill %1 2>/dev/null || true

echo ""
echo "Morgoth is running under PM2."
echo "Useful commands:"
echo "  pm2 status            — check state"
echo "  pm2 logs morgoth      — tail logs"
echo "  pm2 restart morgoth   — restart"
echo "  pm2 stop morgoth      — stop"
echo "  pm2 monit             — live dashboard"
