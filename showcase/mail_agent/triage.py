"""Picks the main model and effort for a run from how hard the request looks.

A tiny Haiku call with every tool and customization off, so it costs little.
Fable is never chosen; on any failure the run falls back to Sonnet.
"""

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

from .config import Config
from .runner import NO_WINDOW

log = logging.getLogger(__name__)

MODELS = ("haiku", "sonnet", "opus")
EFFORTS = ("low", "medium", "high")
FALLBACK_MODEL, FALLBACK_EFFORT = "sonnet", "medium"
MAX_TEXT = 6000

SYSTEM = """\
Bir yazılım görevinin zorluğunu değerlendirip Claude Code için en verimli ana modeli seçiyorsun. Abonelik kotası sınırlı; gereğinden güçlü model seçmek israftır.
- haiku: tek dosyalık küçük betik, basit düzeltme, metin/dosya işi, açık talimatlı mekanik iş.
- sonnet: tipik çok dosyalı proje, oyun/uygulama prototipi, orta zorlukta hata ayıklama. Emin değilsen sonnet.
- opus: zor algoritma, karmaşık mimari, büyük/hazır kod tabanında derin değişiklik, eşzamanlılık/gerçek zamanlı sistem, güvenlik açısından hassas iş.
effort: low (mekanik), medium (normal), high (dikkat gerektiren zor iş).
reason: Türkçe, tek cümle."""

SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string", "enum": list(MODELS)},
        "effort": {"type": "string", "enum": list(EFFORTS)},
        "reason": {"type": "string"},
    },
    "required": ["model", "effort", "reason"],
}


@dataclass(frozen=True)
class Choice:
    model: str
    effort: str
    reason: str
    cost_usd: float = 0.0


def _parse(stdout: str) -> Choice:
    envelope = json.loads(stdout)
    data = envelope.get("structured_output")
    if not isinstance(data, dict):
        data = json.loads(str(envelope.get("result", "")).strip().strip("`").removeprefix("json"))
    model, effort = data.get("model"), data.get("effort")
    if model not in MODELS or effort not in EFFORTS:
        raise ValueError(f"geçersiz seçim: {data}")
    return Choice(model, effort, str(data.get("reason", "")).strip(), envelope.get("total_cost_usd") or 0.0)


def choose(cfg: Config, request: str) -> Choice:
    claude = shutil.which(cfg.claude_command) or cfg.claude_command
    cmd = [
        claude, "-p", "Görev:\n\n" + request[:MAX_TEXT],
        "--model", "haiku",
        "--system-prompt", SYSTEM,
        "--json-schema", json.dumps(SCHEMA),
        "--output-format", "json",
        "--tools", "",
        "--safe-mode",
        "--no-session-persistence",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=180,
                             stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        choice = _parse(out.stdout)
    except Exception as exc:
        log.warning("Model seçimi yapılamadı (%s), %s kullanılacak", exc, FALLBACK_MODEL)
        return Choice(FALLBACK_MODEL, FALLBACK_EFFORT, "otomatik seçim başarısız, varsayılan kullanıldı")
    log.info("Model seçimi: %s/%s — %s", choice.model, choice.effort, choice.reason)
    return choice
