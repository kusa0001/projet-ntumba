#!/usr/bin/env python3
"""
Serveur web pour visualiser les logs de monitor.log en temps réel.
Utilise Flask + Server-Sent Events (SSE) pour les notifications live.
"""

import os
import json
import time
import queue
import threading

from flask import Flask, Response, render_template, jsonify

app = Flask(__name__)

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "monitor.log")

# Liste des queues — une par client SSE connecté
_subscribers: list[queue.Queue] = []
_subscribers_lock = threading.Lock()


def parse_log_line(line: str) -> dict | None:
    """
    Parse une ligne du fichier log au format :
        2024-01-15 10:30:45,123 - LEVEL - message

    Returns:
        dict avec timestamp, level, message, raw — ou None si ligne vide.
    """
    line = line.strip()
    if not line:
        return None

    parts = line.split(" - ", 2)
    if len(parts) == 3:
        timestamp, level, message = parts
        return {
            "timestamp": timestamp.strip(),
            "level": level.strip().upper(),
            "message": message.strip(),
            "raw": line,
        }
    # Ligne non standard : on la garde telle quelle
    return {"timestamp": "", "level": "INFO", "message": line, "raw": line}


def _broadcast(data: str) -> None:
    """Envoie un message JSON à tous les clients SSE connectés."""
    with _subscribers_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def tail_log() -> None:
    """
    Thread daemon qui surveille monitor.log et pousse les nouvelles lignes
    vers tous les clients SSE via leurs queues.
    """
    last_size = 0

    while True:
        try:
            if os.path.exists(LOG_FILE):
                current_size = os.path.getsize(LOG_FILE)
                if current_size > last_size:
                    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_size)
                        new_lines = f.readlines()
                    last_size = current_size

                    for line in new_lines:
                        parsed = parse_log_line(line)
                        if parsed:
                            _broadcast(json.dumps(parsed))

                elif current_size < last_size:
                    # Fichier tronqué / rotation
                    last_size = 0
        except Exception:
            pass

        time.sleep(0.4)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/logs")
def get_logs():
    """Retourne tout l'historique du fichier log en JSON."""
    logs = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parsed = parse_log_line(line)
                if parsed:
                    logs.append(parsed)
    return jsonify(logs)


@app.route("/api/stream")
def stream():
    """
    Endpoint SSE : envoie les nouvelles lignes de log en temps réel.
    Le client s'abonne et reçoit chaque événement sous forme de JSON.
    """
    q: queue.Queue = queue.Queue(maxsize=200)
    with _subscribers_lock:
        _subscribers.append(q)

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=25)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    # Heartbeat pour maintenir la connexion ouverte
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            with _subscribers_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/stats")
def get_stats():
    """Retourne les compteurs par niveau pour les cartes de stats."""
    counts = {"INFO": 0, "WARNING": 0, "ERROR": 0, "TOTAL": 0}
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parsed = parse_log_line(line)
                if parsed:
                    counts["TOTAL"] += 1
                    lvl = parsed["level"]
                    if lvl in counts:
                        counts[lvl] += 1
    return jsonify(counts)


if __name__ == "__main__":
    # Démarrer le thread de surveillance du fichier log
    watcher = threading.Thread(target=tail_log, daemon=True)
    watcher.start()

    print("\n  File System Monitor — Interface Web")
    print("  ➜  http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
