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
    return {"timestamp": "", "level": "INFO", "message": line, "raw": line}


def _is_detail_line(msg: str) -> bool:
    """Une ligne de détail est une sous-ligne d'alerte (commence par ' - ')."""
    return msg.startswith(" - ") or msg.startswith("  - ")


def group_logs(parsed_lines: list) -> list:
    """
    Regroupe les lignes [ALERTE] avec leurs lignes de détail suivantes.
    Les entrées groupées ont un champ 'details' (liste de strings).
    Les lignes de détail orphelines sont ignorées (déjà consommées).
    """
    groups = []
    i = 0
    while i < len(parsed_lines):
        line = parsed_lines[i]
        msg = line.get("message", "")

        if "[ALERTE]" in msg:
            details = []
            j = i + 1
            while j < len(parsed_lines):
                next_msg = parsed_lines[j].get("message", "")
                if _is_detail_line(next_msg):
                    details.append(next_msg.strip())
                    j += 1
                else:
                    break
            entry = dict(line)
            if details:
                entry["details"] = details
            groups.append(entry)
            i = j

        elif _is_detail_line(msg):
            # Ligne déjà consommée par un groupe précédent — ignorer
            i += 1

        else:
            groups.append(line)
            i += 1

    return groups


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
    Thread daemon qui surveille monitor.log, groupe les lignes [ALERTE]
    avec leurs détails, puis pousse vers tous les clients SSE.
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

                    parsed = [p for line in new_lines if (p := parse_log_line(line))]

                    # Si un [ALERTE] est détecté, attendre un peu pour que
                    # les lignes de détail suivantes soient écrites
                    has_alert = any("[ALERTE]" in p.get("message", "") for p in parsed)
                    if has_alert:
                        time.sleep(0.2)
                        current_size2 = os.path.getsize(LOG_FILE)
                        if current_size2 > last_size:
                            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                                f.seek(last_size)
                                more = f.readlines()
                            last_size = current_size2
                            parsed += [p for line in more if (p := parse_log_line(line))]

                    for entry in group_logs(parsed):
                        _broadcast(json.dumps(entry))

                elif current_size < last_size:
                    last_size = 0

        except Exception:
            pass

        time.sleep(0.4)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/logs")
def get_logs():
    """Retourne tout l'historique du fichier log, groupé, en JSON."""
    raw = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parsed = parse_log_line(line)
                if parsed:
                    raw.append(parsed)
    return jsonify(group_logs(raw))


@app.route("/api/stats")
def get_stats():
    """Retourne les compteurs par niveau (les lignes de détail ne comptent pas)."""
    counts = {"INFO": 0, "WARNING": 0, "ERROR": 0, "TOTAL": 0}
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parsed = parse_log_line(line)
                if parsed and not _is_detail_line(parsed["message"]):
                    counts["TOTAL"] += 1
                    lvl = parsed["level"]
                    if lvl in counts:
                        counts[lvl] += 1
    return jsonify(counts)


@app.route("/api/stream")
def stream():
    """
    Endpoint SSE : envoie les nouvelles entrées groupées en temps réel.
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


if __name__ == "__main__":
    watcher = threading.Thread(target=tail_log, daemon=True)
    watcher.start()

    print("\n  File System Monitor — Interface Web")
    print("  ➜  http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
