# -*- coding: utf-8 -*-
"""
Autonomer n8n Workflow Architect für lokales, kostenloses n8n (Community Edition).
- Unterstützt volle REST-API (/api/v1/workflows) über N8N_BASE_URL & N8N_API_KEY
- Automatischer Webhook-Fallback ohne API-Key (lokale Workflow-JSONs + direkter Webhook-Aufruf)
- JSON-Validierung & automatische Selbstkorrektur bei Schema-/API-Fehlern
- Live-Visualisierung im 3D-Neural-Core (goldene Leiterbahn-Impulse) & HUD-Kachel
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Any, Optional
import uuid

import requests

BASE_DIR = Path(__file__).resolve().parent
WORKFLOWS_DIR = BASE_DIR / "n8n_workflows"


def _emit_n8n_visual(
    step_label: str,
    *,
    brain_state: str = "n8n",
    intensity: float = 0.92,
    node_name: str = "",
    code_preview: str = "",
    status_snapshot: Optional[dict[str, Any]] = None,
) -> None:
    """Meldet n8n-Schritte live an das 3D-Gehirn, Sub-Fenster A und die n8n-Bento-Kachel."""
    try:
        from gui import get_active_gui

        gui_inst = get_active_gui()
        if gui_inst is None:
            return
        gui_inst.emit_brain_activity(
            state=brain_state,
            intensity=intensity,
            detail=step_label[:80],
        )
        gui_inst.emit_neural_thought(
            category="n8n",
            tool=node_name or "n8n-architect",
            summary=step_label,
            code=code_preview[:460] if code_preview else "",
        )
        if status_snapshot is not None:
            gui_inst._broadcast({"type": "n8n_status", "n8n": status_snapshot})
    except Exception:
        pass


def get_n8n_config() -> dict[str, str]:
    base_url = (os.getenv("N8N_BASE_URL", "http://localhost:5678") or "http://localhost:5678").strip().rstrip("/")
    api_key = (os.getenv("N8N_API_KEY", "") or "").strip()
    webhook_url = (os.getenv("N8N_WEBHOOK_URL", "") or "").strip()
    return {
        "base_url": base_url,
        "api_key": api_key,
        "webhook_url": webhook_url,
    }


def _slugify(text: str, fallback: str = "jarvis-flow") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:42].strip("-") or fallback)


def check_n8n_health() -> dict[str, Any]:
    cfg = get_n8n_config()
    url = f"{cfg['base_url']}/healthz"
    online = False
    try:
        r = requests.get(url, timeout=1.2)
        online = r.status_code < 500
    except Exception:
        online = False

    has_key = bool(cfg["api_key"])
    return {
        "online": online,
        "base_url": cfg["base_url"],
        "has_api_key": has_key,
        "mode": "REST-API" if (online and has_key) else ("WEBHOOK-FALLBACK" if online else "OFFLINE (LOCAL JSON)"),
        "webhook_default": cfg["webhook_url"],
    }


def _build_default_workflow_template(goal: str, name: str = "", webhook_slug: str = "") -> dict[str, Any]:
    """Erstellt ein garantiertes, n8n-v1-valides Workflow-JSON mit Webhook-Trigger, Verarbeitung & Antwort."""
    clean_name = (name or "").strip() or f"Jarvis · {goal[:48].strip() or 'Automation'}"
    slug = webhook_slug or _slugify(clean_name, "jarvis-hook")
    q = (goal or "").lower()

    nodes: list[dict[str, Any]] = [
        {
            "id": str(uuid.uuid4()),
            "name": "Jarvis Webhook Trigger",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [220, 300],
            "webhookId": slug,
            "parameters": {
                "httpMethod": "POST",
                "path": slug,
                "responseMode": "responseNode",
                "options": {},
            },
        }
    ]

    mid_node_name = "Jarvis Logic Node"
    if any(k in q for k in ("api", "http", "wetter", "news", "fetch", "url", "abruf")):
        url_m = re.search(r"(https?://[^\s\"']+)", goal)
        target_url = url_m.group(1) if url_m else "http://127.0.0.1:8000/api/state"
        mid_node_name = "HTTP Request"
        nodes.append(
            {
                "id": str(uuid.uuid4()),
                "name": mid_node_name,
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [500, 300],
                "parameters": {
                    "url": target_url,
                    "method": "GET",
                    "options": {},
                },
            }
        )
    else:
        safe_goal = goal.replace('"', '\\"').replace("\n", " ")[:180]
        js_code = (
            "const payload = $input.first()?.json || {};\n"
            f"const goal = \"{safe_goal}\";\n"
            "return [{ json: { ok: true, workflow: goal, received: payload.body || payload, ts: new Date().toISOString() } }];"
        )
        nodes.append(
            {
                "id": str(uuid.uuid4()),
                "name": mid_node_name,
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [500, 300],
                "parameters": {
                    "jsCode": js_code,
                },
            }
        )

    nodes.append(
        {
            "id": str(uuid.uuid4()),
            "name": "Respond to Jarvis",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1.1,
            "position": [780, 300],
            "parameters": {
                "respondWith": "firstIncomingItem",
                "options": {},
            },
        }
    )

    connections = {
        "Jarvis Webhook Trigger": {
            "main": [[{"node": mid_node_name, "type": "main", "index": 0}]]
        },
        mid_node_name: {
            "main": [[{"node": "Respond to Jarvis", "type": "main", "index": 0}]]
        },
    }

    return {
        "name": clean_name,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
    }


def validate_and_repair_workflow_json(raw: Any, goal: str, fallback_name: str = "") -> tuple[dict[str, Any], list[str]]:
    """
    Prüft n8n-Workflow-JSON strikt auf Pflichtfelder, Node-Typen, Positionen & Verbindungen
    und repariert Fehler selbstständig vor dem API-Upload.
    """
    fixes: list[str] = []
    if not isinstance(raw, dict):
        fixes.append("Workflow-Root war kein JSON-Objekt -> Standard-Template erzeugt.")
        return _build_default_workflow_template(goal, name=fallback_name), fixes

    wf = dict(raw)
    # Read-Only-Felder entfernen, die bei POST /api/v1/workflows zu 400 führen können
    for ro_key in ("id", "active", "createdAt", "updatedAt", "versionId", "tags"):
        if ro_key in wf:
            wf.pop(ro_key, None)
            fixes.append(f"Read-only Feld '{ro_key}' für v1-API bereinigt.")

    if not str(wf.get("name") or "").strip():
        wf["name"] = fallback_name.strip() or f"Jarvis · {goal[:44].strip() or 'Automation'}"
        fixes.append("Workflow-Name ergänzt.")

    nodes = wf.get("nodes")
    if not isinstance(nodes, list) or len(nodes) == 0:
        fixes.append("Fehlende oder leere Nodes-Liste -> Basis-Nodes erstellt.")
        return _build_default_workflow_template(goal, name=str(wf["name"])), fixes

    cleaned_nodes: list[dict[str, Any]] = []
    has_webhook = False
    has_respond = False
    slug = _slugify(str(wf["name"]), "jarvis-hook")

    for idx, n in enumerate(nodes):
        if not isinstance(n, dict):
            fixes.append(f"Ungültigen Node #{idx} verworfen.")
            continue
        node = dict(n)
        if not str(node.get("id") or "").strip():
            node["id"] = str(uuid.uuid4())
        if not str(node.get("name") or "").strip():
            node["name"] = f"Node {idx + 1}"
            fixes.append(f"Node-Name für #{idx} ergänzt.")
        if not str(node.get("type") or "").strip():
            node["type"] = "n8n-nodes-base.noOp"
            fixes.append(f"Node-Typ für '{node['name']}' korrigiert.")
        if "typeVersion" not in node:
            node["typeVersion"] = 1
        pos = node.get("position")
        if not (isinstance(pos, list) and len(pos) == 2):
            node["position"] = [220 + idx * 260, 300]
            fixes.append(f"Koordinaten für '{node['name']}' gesetzt.")
        if not isinstance(node.get("parameters"), dict):
            node["parameters"] = {}
        ntype = str(node["type"])
        if "webhook" in ntype.lower() and "respond" not in ntype.lower():
            has_webhook = True
            node["parameters"].setdefault("path", slug)
            node["parameters"].setdefault("httpMethod", "POST")
            node["parameters"].setdefault("responseMode", "responseNode")
        if "respondtowebhook" in ntype.lower():
            has_respond = True
        cleaned_nodes.append(node)

    if not cleaned_nodes:
        fixes.append("Keine gültigen Nodes übrig -> Standard-Workflow erzeugt.")
        return _build_default_workflow_template(goal, name=str(wf["name"]), webhook_slug=slug), fixes

    if not has_webhook:
        trig = {
            "id": str(uuid.uuid4()),
            "name": "Jarvis Webhook Trigger",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [160, 300],
            "webhookId": slug,
            "parameters": {"httpMethod": "POST", "path": slug, "responseMode": "responseNode", "options": {}},
        }
        cleaned_nodes.insert(0, trig)
        fixes.append("Fehlenden Webhook-Trigger automatisch ergänzt.")

    if not has_respond:
        last_x = max(int(n["position"][0]) for n in cleaned_nodes)
        resp_node = {
            "id": str(uuid.uuid4()),
            "name": "Respond to Jarvis",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1.1,
            "position": [last_x + 260, 300],
            "parameters": {"respondWith": "firstIncomingItem", "options": {}},
        }
        cleaned_nodes.append(resp_node)
        fixes.append("Fehlenden Respond-to-Webhook-Node ergänzt.")

    conns = wf.get("connections")
    node_names = [n["name"] for n in cleaned_nodes]
    if not isinstance(conns, dict) or not conns:
        conns = {}
        for i in range(len(node_names) - 1):
            conns[node_names[i]] = {"main": [[{"node": node_names[i + 1], "type": "main", "index": 0}]]}
        fixes.append("Node-Verbindungen linear verknüpft.")

    wf["nodes"] = cleaned_nodes
    wf["connections"] = conns
    if not isinstance(wf.get("settings"), dict):
        wf["settings"] = {"executionOrder": "v1"}
    else:
        wf["settings"].setdefault("executionOrder", "v1")
    return wf, fixes


def _extract_json_block(text: str) -> Any:
    raw = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    if m:
        raw = m.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            raw = raw[start : end + 1]
    return json.loads(raw)


def _extract_webhook_slug(wf: dict[str, Any]) -> str:
    for node in wf.get("nodes", []) or []:
        if isinstance(node, dict) and "webhook" in str(node.get("type", "")).lower() and "respond" not in str(node.get("type", "")).lower():
            p = str((node.get("parameters") or {}).get("path") or "").strip("/")
            if p:
                return p
    return _slugify(str(wf.get("name") or "jarvis-hook"))


def _save_local_workflow_copy(wf: dict[str, Any]) -> Path:
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slugify(str(wf.get("name") or "jarvis-flow"))
    out_path = WORKFLOWS_DIR / f"{slug}.json"
    out_path.write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def list_local_saved_workflows() -> list[dict[str, Any]]:
    if not WORKFLOWS_DIR.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(WORKFLOWS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            out.append(
                {
                    "id": f.stem,
                    "name": data.get("name", f.stem),
                    "active": False,
                    "webhook_slug": _extract_webhook_slug(data),
                    "node_count": len(data.get("nodes", []) or []),
                    "local_file": str(f),
                }
            )
        except Exception:
            pass
    return out


class N8nArchitect:
    """Zentraler n8n Workflow-Builder, Verwalter & Executor für Jarvis."""

    def __init__(self) -> None:
        self._last_event: str = "Bereit (0 EUR Community-Edition)"
        self._recent_nodes: list[str] = []

    def get_status(self) -> dict[str, Any]:
        from beta_guard import BETA_GUARD

        if BETA_GUARD.is_expired():
            return {
                **BETA_GUARD.get_api_expired_payload(),
                "online": False,
                "mode": "EXPIRED",
                "workflow_count": 0,
                "workflows": [],
            }
        health = check_n8n_health()
        saved = list_local_saved_workflows()
        workflows: list[dict[str, Any]] = []
        if health["online"] and health["has_api_key"]:
            try:
                cfg = get_n8n_config()
                r = requests.get(
                    f"{cfg['base_url']}/api/v1/workflows",
                    headers={"X-N8N-API-KEY": cfg["api_key"]},
                    timeout=1.6,
                )
                if r.status_code == 200:
                    data = r.json().get("data", []) or []
                    for item in data[:12]:
                        workflows.append(
                            {
                                "id": str(item.get("id", "")),
                                "name": str(item.get("name", "Workflow")),
                                "active": bool(item.get("active", False)),
                                "node_count": len(item.get("nodes", []) or []),
                                "webhook_slug": _extract_webhook_slug(item),
                            }
                        )
            except Exception:
                pass
        if not workflows:
            workflows = saved[:12]

        return {
            **health,
            "workflow_count": len(workflows),
            "workflows": workflows,
            "recent_nodes": list(self._recent_nodes[-6:]),
            "last_event": self._last_event,
            "updated_at": datetime.now().strftime("%H:%M:%S"),
        }

    def create_workflow(
        self,
        goal: str,
        name: str = "",
        activate: bool = True,
        brain: Any = None,
    ) -> dict[str, Any]:
        from beta_guard import BETA_GUARD

        if BETA_GUARD.is_expired():
            return {"ok": False, **BETA_GUARD.get_api_expired_payload()}
        cfg = get_n8n_config()
        clean_goal = (goal or "").strip() or "Allgemeiner Jarvis Webhook-Workflow"
        _emit_n8n_visual(f"n8n-Architect: Entwerfe Workflow für '{clean_goal[:48]}'", node_name="Architect Planner")

        raw_wf: Any = None
        if brain is not None and hasattr(brain, "ask"):
            try:
                prompt = (
                    "Erstelle ein valides n8n v1 Workflow-JSON (nur reines JSON, keine Erklärung) für folgendes Ziel:\n"
                    f"Ziel: {clean_goal}\n"
                    "Pflichtstruktur: {\"name\": str, \"nodes\": [...], \"connections\": {...}, \"settings\": {\"executionOrder\": \"v1\"}}.\n"
                    "Der erste Node muss n8n-nodes-base.webhook (httpMethod POST, responseMode responseNode) sein "
                    "und der letzte Node n8n-nodes-base.respondToWebhook."
                )
                res = brain.ask(prompt, use_memory=False)
                raw_wf = _extract_json_block(getattr(res, "text", str(res)))
            except Exception:
                raw_wf = None

        if raw_wf is None:
            raw_wf = _build_default_workflow_template(clean_goal, name=name)

        wf, fixes = validate_and_repair_workflow_json(raw_wf, clean_goal, fallback_name=name)
        self._recent_nodes = [str(n.get("name", "")) for n in wf.get("nodes", [])][:6]
        slug = _extract_webhook_slug(wf)
        local_path = _save_local_workflow_copy(wf)
        webhook_url = f"{cfg['base_url']}/webhook/{slug}"

        _emit_n8n_visual(
            f"Validiert ({len(wf['nodes'])} Nodes) -> '{wf['name']}'",
            node_name="JSON Validator",
            code_preview=json.dumps(wf, indent=2, ensure_ascii=False)[:440],
        )

        # Ohne API-Key -> automatischer Webhook-Fallback
        if not cfg["api_key"]:
            self._last_event = f"Webhook-Fallback erstellt: {wf['name']} ({slug})"
            snap = self.get_status()
            _emit_n8n_visual(self._last_event, brain_state="idle", intensity=0.45, node_name="Webhook Fallback", status_snapshot=snap)
            return {
                "ok": True,
                "mode": "webhook_fallback",
                "workflow_name": wf["name"],
                "webhook_url": webhook_url,
                "local_file": str(local_path),
                "fixes": fixes,
                "message": (
                    f"Workflow '{wf['name']}' als n8n-JSON ({local_path.name}) im Webhook-Fallback erstellt. "
                    f"Sobald du in den Einstellungen einen N8N_API_KEY hinterlegst, lade ich ihn vollautomatisch per REST-API hoch."
                ),
            }

        headers = {"X-N8N-API-KEY": cfg["api_key"], "Content-Type": "application/json"}
        api_url = f"{cfg['base_url']}/api/v1/workflows"
        created: Optional[dict[str, Any]] = None
        last_err = ""

        for attempt in range(2):
            try:
                r = requests.post(api_url, headers=headers, json=wf, timeout=5.0)
                if r.status_code in (200, 201):
                    created = r.json()
                    break
                last_err = f"HTTP {r.status_code}: {r.text[:220]}"
                if attempt == 0:
                    # Automatische Selbstkorrektur bei API-400
                    _emit_n8n_visual(f"n8n API Korrektur ({last_err[:60]})...", node_name="Self-Healing")
                    if brain is not None and hasattr(brain, "ask"):
                        try:
                            fix_res = brain.ask(
                                f"Korrigiere dieses n8n Workflow-JSON anhand der Fehlermeldung '{last_err}'. Gib NUR valides JSON zurück:\n{json.dumps(wf)[:1800]}",
                                use_memory=False,
                            )
                            wf, more_fixes = validate_and_repair_workflow_json(_extract_json_block(fix_res.text), clean_goal, fallback_name=wf["name"])
                            fixes.extend(more_fixes)
                            continue
                        except Exception:
                            pass
                    # Harter Fallback auf garantiertes Basis-Template mit selber Bezeichnung
                    wf = _build_default_workflow_template(clean_goal, name=wf["name"], webhook_slug=slug)
                    fixes.append("Nach API-Fehler auf Basis-Template selbstkorrigiert.")
            except Exception as exc:
                last_err = str(exc)
                break

        if created is None:
            self._last_event = f"Lokal gespeichert (API Hinweis: {last_err[:48]})"
            snap = self.get_status()
            _emit_n8n_visual(self._last_event, brain_state="idle", intensity=0.4, status_snapshot=snap)
            return {
                "ok": True,
                "mode": "webhook_fallback",
                "workflow_name": wf["name"],
                "webhook_url": webhook_url,
                "local_file": str(local_path),
                "fixes": fixes,
                "api_error": last_err,
                "message": (
                    f"Workflow '{wf['name']}' unter {local_path.name} gespeichert (Webhook-Fallback aktiv, "
                    f"da n8n-API nicht erreichbar war: {last_err[:80]})."
                ),
            }

        wf_id = str(created.get("id", ""))
        activated = False
        if activate and wf_id:
            try:
                ar = requests.post(f"{api_url}/{wf_id}/activate", headers=headers, timeout=4.0)
                activated = ar.status_code in (200, 201)
            except Exception:
                activated = False

        self._last_event = f"Aktiviert: {wf['name']} (ID {wf_id})" if activated else f"Erstellt: {wf['name']} (ID {wf_id})"
        snap = self.get_status()
        _emit_n8n_visual(self._last_event, brain_state="idle", intensity=0.5, node_name="n8n REST-API", status_snapshot=snap)
        return {
            "ok": True,
            "mode": "rest_api",
            "id": wf_id,
            "activated": activated,
            "workflow_name": wf["name"],
            "webhook_url": webhook_url,
            "local_file": str(local_path),
            "fixes": fixes,
            "message": f"n8n-Workflow '{wf['name']}' (ID {wf_id}) erstellt" + (" und aktiviert." if activated else "."),
        }

    def list_and_manage(self, action: str = "list", workflow_id: str = "") -> dict[str, Any]:
        act = (action or "list").strip().lower()
        cfg = get_n8n_config()
        headers = {"X-N8N-API-KEY": cfg["api_key"]} if cfg["api_key"] else {}

        if act in ("list", "status"):
            snap = self.get_status()
            names = [f"{w['name']} ({'AKTIV' if w.get('active') else 'BEREIT'})" for w in snap["workflows"]]
            msg = (
                f"n8n ({snap['mode']}): {len(names)} Workflows -> {', '.join(names[:6])}."
                if names
                else f"n8n ({snap['mode']}): Noch keine Workflows gefunden."
            )
            _emit_n8n_visual(msg, brain_state="idle", intensity=0.35, node_name="Workflow Manager", status_snapshot=snap)
            return {"ok": True, **snap, "message": msg}

        if not cfg["api_key"]:
            return {
                "ok": False,
                "message": f"Für '{act}' wird ein N8N_API_KEY in den Einstellungen benötigt (Webhook-Fallback läuft auch ohne Key).",
            }
        if not workflow_id:
            return {"ok": False, "message": "Bitte eine Workflow-ID angeben."}

        try:
            base = f"{cfg['base_url']}/api/v1/workflows/{workflow_id}"
            if act == "activate":
                r = requests.post(f"{base}/activate", headers=headers, timeout=4.0)
            elif act == "deactivate":
                r = requests.post(f"{base}/deactivate", headers=headers, timeout=4.0)
            elif act == "delete":
                r = requests.delete(base, headers=headers, timeout=4.0)
            else:
                return {"ok": False, "message": f"Unbekannte Aktion: {act}"}
            ok = r.status_code in (200, 201, 204)
            snap = self.get_status()
            msg = f"n8n Aktion '{act}' für ID {workflow_id}: {'erfolgreich' if ok else f'HTTP {r.status_code}'}."
            self._last_event = msg
            _emit_n8n_visual(msg, brain_state="n8n" if ok else "error", intensity=0.8, node_name="Workflow Manager", status_snapshot=snap)
            return {"ok": ok, "status_code": r.status_code, "message": msg}
        except Exception as exc:
            return {"ok": False, "message": f"n8n API-Fehler: {exc}"}

    def run_workflow(self, target: str = "", payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        from beta_guard import BETA_GUARD

        if BETA_GUARD.is_expired():
            return {"ok": False, **BETA_GUARD.get_api_expired_payload()}
        cfg = get_n8n_config()
        clean_target = (target or "").strip()
        body = payload if isinstance(payload, dict) else {"source": "jarvis", "ts": datetime.now().isoformat()}

        # Bestimme Webhook-URL: explizite URL > N8N_WEBHOOK_URL > /webhook/<slug>
        if clean_target.startswith(("http://", "https://")):
            hook_url = clean_target
        elif clean_target:
            slug = clean_target.lstrip("/")
            if slug.startswith("webhook/"):
                hook_url = f"{cfg['base_url']}/{slug}"
            else:
                hook_url = f"{cfg['base_url']}/webhook/{_slugify(slug)}"
        elif cfg["webhook_url"]:
            hook_url = cfg["webhook_url"]
        else:
            saved = list_local_saved_workflows()
            slug = saved[0]["webhook_slug"] if saved else "jarvis-hook"
            hook_url = f"{cfg['base_url']}/webhook/{slug}"

        _emit_n8n_visual(f"Starte n8n-Webhook: {hook_url}", brain_state="n8n", intensity=0.96, node_name="Webhook Trigger")
        try:
            r = requests.post(hook_url, json=body, timeout=8.0)
            ok = r.status_code < 400
            resp_preview = r.text[:240] if r.text else f"HTTP {r.status_code}"
            self._last_event = f"Ausgeführt ({r.status_code}): {hook_url.split('/')[-1]}"
            snap = self.get_status()
            _emit_n8n_visual(
                f"n8n Antwort ({r.status_code}): {resp_preview[:100]}",
                brain_state="idle",
                intensity=0.45,
                node_name="Respond to Webhook",
                code_preview=resp_preview,
                status_snapshot=snap,
            )
            return {
                "ok": ok,
                "status_code": r.status_code,
                "webhook_url": hook_url,
                "response": resp_preview,
                "message": f"n8n-Workflow über {hook_url} ausgeführt (Status {r.status_code})." if ok else f"Webhook meldete HTTP {r.status_code}: {resp_preview}",
            }
        except Exception as exc:
            self._last_event = f"Webhook nicht erreichbar: {hook_url.split('/')[-1]}"
            snap = self.get_status()
            _emit_n8n_visual(f"n8n Hinweis: {exc}", brain_state="idle", intensity=0.35, node_name="Webhook Trigger", status_snapshot=snap)
            return {
                "ok": False,
                "webhook_url": hook_url,
                "message": f"Konnte Webhook {hook_url} nicht erreichen ({exc}). Läuft lokales n8n über start_n8n.bat / start_n8n.sh?",
            }


N8N_ARCHITECT = N8nArchitect()
