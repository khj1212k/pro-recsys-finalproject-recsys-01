#!/bin/bash

# Project Root
WORKSPACE_DIR="/data/ephemeral/home/pro-recsys-finalproject-recsys-01/ai_workspace"
export AIRFLOW_HOME="$WORKSPACE_DIR/airflow_home"
export PYTHONPATH="$WORKSPACE_DIR:$PYTHONPATH"

# Ensure log directory exists
mkdir -p "$WORKSPACE_DIR/logs"

echo "=========================================="
echo " Starting Airflow Scheduler in Background "
echo "=========================================="
echo "AIRFLOW_HOME: $AIRFLOW_HOME"
echo "Date: $(date)"

# Stop existing scheduler if any (simple kill)
pkill -f "airflow scheduler" || true

# Start Scheduler with nohup
nohup airflow scheduler >> "$WORKSPACE_DIR/logs/scheduler.out" 2>&1 &
SCHEDULER_PID=$!

echo "✅ Scheduler started! (PID: $SCHEDULER_PID)"
echo "   Logs: $WORKSPACE_DIR/logs/scheduler.out"
echo "   The pipeline will run automatically continuously."
echo "   Next run expected at 18:00 UTC."
