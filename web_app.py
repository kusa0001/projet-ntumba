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
import urllib.request

from flask import Flask, Response, render_template, jsonify, request as flask_request

app = Flask(__name__)

LOG_FILE          = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "monitor.log")
DISCORD_CFG_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discord_config.json")

# Liste des queues — une par client SSE connecté
_subscribers: list[queue.Queue] = []
_subscribers_lock = threading.Lock()


# ── Discord config ────────────────────────────────────────────────────────────

def load_discord_config() -> dict:
    if not os.path.exists(DISCORD_CFG_FILE):
        return {"webhook_url": "", "panel_url": "http://localhost:5000"}
    try:
        with open(DISCORD_CFG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"webhook_url": "", "panel_url": "http://localhost:5000"}


def save_discord_config(data: dict) -> None:
    with open(DISCORD_CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def send_discord_alert(entry: dict) -> None:
    """Envoie une notification Discord pour une alerte groupée."""
    cfg = load_discord_config()
    webhook_url = cfg.get("webhook_url", "").strip()
    panel_url   = cfg.get("panel_url", "http://localhost:5000").strip()

    if not webhook_url:
        return

    msg       = entry.get("message", "")
    details   = entry.get("details", [])
    timestamp = entry.get("timestamp", "")

    # Nom du fichier extrait du chemin dans le message
    path_part = msg.split(":")[-1].strip() if ":" in msg else ""
    filename  = os.path.basename(path_part) if path_part else "fichier"

    # Construction de la description de l'embed
    desc_lines = [f"```{msg}```"]
    for d in details:
        desc_lines.append(f"• {d}")
    desc_lines.append(f"\n🔗 **[Voir le panel de surveillance]({panel_url})**")

    payload = {
        "embeds": [{
            "title":       f"🚨 Nouvelle alerte : modification détectée sur `{filename}`",
            "description": "\n".join(desc_lines),
            "color":       0xDC2626,
            "footer":      {"text": f"File System Monitor  •  {timestamp}"},
        }]
    }

    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


# ── Log parsing & grouping ────────────────────────────────────────────────────

def parse_log_line(line: str) -> dict | None:
    """
    Parse une ligne du fichier log au format :
        2024-01-15 10:30:45,123 - LEVEL - message

    Returns:
        dict avec timestamp, level, message, raw, is_detail — ou None si vide.
    """
    line = line.strip()
    if not line:
        return None

    parts = line.split(" - ", 2)
    if len(parts) == 3:
        timestamp, level, raw_msg = parts
        is_detail = raw_msg.startswith(" - ") or raw_msg.startswith("  - ")
        return {
            "timestamp": timestamp.strip(),
            "level":     level.strip().upper(),
            "message":   raw_msg.strip(),
            "raw":       line,
            "is_detail": is_detail,
        }
    return {"timestamp": "", "level": "INFO", "message": line, "raw": line, "is_detail": False}


def _is_detail_line(parsed: dict) -> bool:
    return parsed.get("is_detail", False)


def group_logs(parsed_lines: list) -> list:
    """
    Regroupe les lignes [ALERTE] avec leurs lignes de détail suivantes.
    Les entrées groupées ont un champ 'details' (liste de strings).
    """
    groups = []
    i = 0
    while i < len(parsed_lines):
        line = parsed_lines[i]
        msg  = line.get("message", "")

        if "[ALERTE]" in msg:
            details = []
            j = i + 1
            while j < len(parsed_lines):
                if _is_detail_line(parsed_lines[j]):
                    details.append(parsed_lines[j]["message"])
                    j += 1
                else:
                    break
            entry = dict(line)
            if details:
                entry["details"] = details
            groups.append(entry)
            i = j

        elif _is_detail_line(line):
            i += 1  # déjà consommée par un groupe précédent

        else:
            groups.append(line)
            i += 1

    return groups


# ── SSE broadcast ─────────────────────────────────────────────────────────────

def _broadcast(data: str) -> None:
    with _subscribers_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


# ── Log tailer ────────────────────────────────────────────────────────────────

def tail_log() -> None:
    """
    Thread daemon : surveille monitor.log, groupe les alertes,
    diffuse en SSE et notifie Discord.
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

                    # Si une alerte est détectée, attendre que toutes les lignes
                    # de détail soient écrites avant de grouper
                    if any("[ALERTE]" in p.get("message", "") for p in parsed):
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
                        if "[ALERTE]" in entry.get("message", ""):
                            threading.Thread(
                                target=send_discord_alert, args=(entry,), daemon=True
                            ).start()

                elif current_size < last_size:
                    last_size = 0

        except Exception:
            pass

        time.sleep(0.4)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/logs")
def get_logs():
    """Retourne tout l'historique groupé en JSON."""
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
    counts = {"INFO": 0, "WARNING": 0, "ERROR": 0, "TOTAL": 0}
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parsed = parse_log_line(line)
                if parsed and not _is_detail_line(parsed):
                    counts["TOTAL"] += 1
                    lvl = parsed["level"]
                    if lvl in counts:
                        counts[lvl] += 1
    return jsonify(counts)


@app.route("/api/stream")
def stream():
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
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/api/discord-config", methods=["GET"])
def get_discord_config():
    return jsonify(load_discord_config())


@app.route("/api/discord-config", methods=["POST"])
def set_discord_config():
    data = flask_request.get_json(force=True)
    save_discord_config({
        "webhook_url": data.get("webhook_url", ""),
        "panel_url":   data.get("panel_url",   "http://localhost:5000"),
    })
    return jsonify({"ok": True})


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=tail_log, daemon=True).start()
    print("\n  File System Monitor — Interface Web")
    print("  ➜  http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
