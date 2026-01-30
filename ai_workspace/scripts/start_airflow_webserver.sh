#!/bin/bash

# Project Root
WORKSPACE_DIR="/data/ephemeral/home/pro-recsys-finalproject-recsys-01/ai_workspace"
export AIRFLOW_HOME="$WORKSPACE_DIR/airflow_home"
export PYTHONPATH="$WORKSPACE_DIR:$PYTHONPATH"

# Ensure log directory exists
mkdir -p "$WORKSPACE_DIR/logs"

echo "=========================================="
echo " Starting Airflow Webserver in Background "
echo "=========================================="

# Stop existing webserver if any
pkill -f "airflow webserver" || true

# Start Webserver with nohup on port 8080 binding to all interfaces
nohup airflow webserver -p 8080 --hostname 0.0.0.0 >> "$WORKSPACE_DIR/logs/webserver.out" 2>&1 &
WEBSERVER_PID=$!

echo "✅ Webserver started! (PID: $WEBSERVER_PID)"
echo "   Logs: $WORKSPACE_DIR/logs/webserver.out"
echo "   Access at: http://localhost:8080"
