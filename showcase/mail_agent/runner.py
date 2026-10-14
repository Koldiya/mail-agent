"""Runs one task through headless Claude Code (`claude -p`).

Claude talks back through files in <project>/.agent/ instead of its last
message, because the user's own Stop hooks (devam notu etc.) also run in bot
sessions and can change what the last message is:
  summary.md   -> result mail
  question.md  -> the task waits for an answer (mail or panel)
  outbox/*.md  -> sent as a status mail while the task is still running
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .store import Task

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
AGENT_ROOT = PACKAGE_DIR.parent
AGENT_DIR = ".agent"

SYSTEM_PROMPT = """\
Bu oturumu mail_agent bir e-posta görevi için otomatik açtı; kullanıcı başında değil, seninle sadece mail üzerinden konuşuyor.
Kullanıcıyla iletişim (proje kökündeki .agent/ klasörü):
- İş bitince .agent/summary.md dosyasına gönderene gidecek kısa Türkçe özeti yaz: ne yaptın, nasıl çalıştırılır/kullanılır, varsayımlar. Bu dosya mail olarak gider; son mesajın gitmez.
- İnisiyatif al: küçük ve geri alınabilir kararları (isimler, kütüphane, tasarım ayrıntısı, makul kapsam) kendin ver, README'de "Varsayımlar" altında yaz.
- Sadece gerçekten kritik yerde sor (geri dönüşü zor/pahalı karar, para, hesap/erişim, görevin anlamını değiştiren belirsizlik): soruyu .agent/question.md dosyasına yaz ve turu bitir; cevap (mail veya bilgisayardaki panelden) gelince bu oturumda devam edersin. Soruda seçenekleri, önerini ve cevap gelmezse hangisiyle devam edeceğini yaz. Sormadan önce soruya bağlı olmayan kısımları bitir.
- Uzun işlerde ara durum vermek istersen .agent/outbox/<ad>.md yaz; hemen mail olarak gönderilir.
Çalışma kuralları:
- Görevi gerçekten bitir: kodu çalıştır, test et, hataları düzelt. Sonunda README.md yaz (ne yapıldı, kurulum/çalıştırma, varsayımlar, bilinen eksikler).
- Python projelerinde proje klasöründe venv kullan. Kabuk komutlarında PowerShell sözdizimi kullan (Windows PowerShell 5.1'de && ve || yok).
- Birden fazla adımlı bir projede kökte NOTES.md tut (mimari, çalıştırma, güncel durum); bağlam sıfırlanınca devam buradan yapılır.
- Verimlilik: abonelik kotası sınırlı. Alt ajan (Agent aracı) açarken model parametresini parçanın zorluğuna göre seç: mekanik/kolay (dosya arama, toplu basit düzenleme, boilerplate, test çalıştırma, belge) → "haiku"; normal → "sonnet"; zor (algoritma, mimari, inatçı hata, eşzamanlılık) → "opus". Küçük işler için alt ajan açma, kendin yap.
- .agent/summary.md'nin sonuna "Model kullanımı" başlığıyla hangi kısmı hangi modelle yaptığını ve nedenini 1-3 satırda yaz.
- Görev isterse proje klasörü dışında da çalışabilirsin, ama mail_agent'ın kendi ayar ve veri dosyalarına dokunma."""

FIRST_PROMPT = "TASK.md dosyasındaki görevi baştan sona tamamla. Ekler varsa attachments/ klasöründe."
FOLLOWUP_PROMPT = "Gönderenden yeni mesaj geldi: {path} dosyasını oku ve gereğini yap. İlk görev TASK.md'de."
TIMEOUT_PROMPT = (
    "Sorduğun soruya {hours} saat içinde cevap gelmedi. Makul bir varsayım yap, README'de belirt ve göreve devam et."
)
FRESH_PREFIX = (
    "Bu konuşmanın önceki Claude oturumu çok uzadığı için yeni oturum açıldı. Önce NOTES.md "
    "(varsa), TASK.md ve messages/ klasörünü okuyup durumu anla. Sonra: "
)
INTERRUPTED_PROMPT = (
    "Önceki çalışma yarıda kesildi (durdurma, limit veya yeniden başlatma). Mevcut duruma bak ve şu işi kaldığın "
    "yerden bitir: {prompt}"
)

RATE_LIMIT_PATTERN = re.compile(r"usage limit|hit your limit|rate limit|limit reached|resets", re.I)
# The agent runs under pythonw; without this every child pops a console window.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
ZIP_SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build", AGENT_DIR}


@dataclass
class RunResult:
    status: str  # done | waiting | failed | rate_limited | stopped | timeout
    summary: str
    cost_usd: float | None = None
    context_tokens: int | None = None
    question: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    limit_used: float | None = None
    model: str | None = None
    model_usage: dict | None = None


def log_path(cfg: Config, task: Task) -> Path:
    return cfg.logs_dir / f"task-{task.id}.jsonl"


def bot_settings() -> str:
    """Added on top of the user's own settings for every bot session."""
    python = Path(sys.executable).as_posix()
    guard = (PACKAGE_DIR / "guard.py").as_posix()
    hook = {"type": "command", "command": f'"{python}" "{guard}"'}
    matcher = "Write|Edit|MultiEdit|NotebookEdit|Read|Glob|Grep|Bash|PowerShell|Agent|Task"
    return json.dumps({
        "hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [hook]}]},
        # claude-mem injects memories of unrelated projects into the bot's context.
        "enabledPlugins": {"claude-mem@thedotmack": False},
    })


def build_command(cfg: Config, session_id: str, prompt: str, resume: bool, model: str, effort: str) -> list[str]:
    claude = shutil.which(cfg.claude_command) or cfg.claude_command
    cmd = [
        claude,
        "-p",
        prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--append-system-prompt", SYSTEM_PROMPT,
        "--settings", bot_settings(),
    ]
    cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if cfg.max_budget_usd > 0:
        cmd += ["--max-budget-usd", f"{cfg.max_budget_usd:g}"]
    return cmd


def session_exists(session_id: str) -> bool:
    """Claude Code keeps transcripts in ~/.claude/projects/<encoded cwd>/<id>.jsonl;
    reusing --session-id for an existing one fails, so we must --resume instead."""
    projects = Path.home() / ".claude" / "projects"
    return any(projects.glob(f"*/{session_id}.jsonl"))


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW
        )
    else:
        proc.kill()
    proc.wait()


@dataclass
class LogStats:
    result: dict | None = None
    context: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    model: str | None = None
    model_usage: dict | None = None
    first_limit: dict | None = None
    last_limit: dict | None = None

    @property
    def limit_used(self) -> float | None:
        """Growth of the 5-hour window during this run (None if it reset meanwhile)."""
        if not self.first_limit or not self.last_limit:
            return None
        a, b = self.first_limit.get("five_hour") or {}, self.last_limit.get("five_hour") or {}
        if a.get("resetsAt") != b.get("resetsAt") or a.get("utilization") is None or b.get("utilization") is None:
            return None
        return max(0.0, b["utilization"] - a["utilization"])


def limit_windows(event: dict) -> dict | None:
    """{'five_hour': {'utilization': 0..1, 'resetsAt': epoch}, 'seven_day': {...}} from a rate_limit_event."""
    if event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info") or {}
    windows = info.get("unifiedWindows")
    if not windows and info.get("rateLimitType") and info.get("utilization") is not None:
        windows = {info["rateLimitType"]: {"utilization": info["utilization"], "resetsAt": info.get("resetsAt")}}
    return windows or None


def read_events(log_file: Path, offset: int) -> tuple[int, list[dict]]:
    """JSON events from whole lines appended since `offset`."""
    if not log_file.exists():
        return offset, []
    with log_file.open("rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1
    events = []
    for raw in data[:end].splitlines():
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return offset + end, events


def collect_stats(stats: LogStats, events: list[dict]) -> None:
    for event in events:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            stats.model = event.get("model") or stats.model
        elif kind == "result":
            stats.result = event
            usage = event.get("usage") or {}
            stats.tokens_in = sum(
                usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
            stats.tokens_out = usage.get("output_tokens")
            stats.model_usage = event.get("modelUsage") or None
        elif kind == "assistant" and not event.get("parent_tool_use_id"):
            usage = event.get("message", {}).get("usage") or {}
            stats.context = sum(
                usage.get(k) or 0
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            ) or stats.context
        elif windows := limit_windows(event):
            stats.first_limit = stats.first_limit or windows
            stats.last_limit = windows


def _take(path: Path) -> str | None:
    """Reads and removes a message file Claude left for us."""
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    path.unlink()
    return text or None


def _flush_outbox(agent_dir: Path, on_outbox: Callable[[str], None]) -> None:
    for path in sorted((agent_dir / "outbox").glob("*.md")):
        text = _take(path)
        if text:
            try:
                on_outbox(text)
            except Exception:
                log.exception("Ara durum maili gönderilemedi")


def run_task(
    cfg: Config,
    task: Task,
    protected: list[Path],
    on_outbox: Callable[[str], None] = lambda text: None,
    on_limits: Callable[[dict], None] = lambda windows: None,
    model: str = "",
    effort: str = "",
) -> RunResult:
    log_file = log_path(cfg, task)
    stop_file = cfg.stop_dir / str(task.id)
    stop_file.unlink(missing_ok=True)
    agent_dir = task.project_dir / AGENT_DIR
    (agent_dir / "outbox").mkdir(parents=True, exist_ok=True)
    for stale in ("summary.md", "question.md"):
        (agent_dir / stale).unlink(missing_ok=True)

    env = os.environ.copy()
    env["MAIL_AGENT_TASK"] = str(task.id)
    env["MAIL_AGENT_PROJECT_DIR"] = str(task.project_dir)
    env["MAIL_AGENT_PROTECTED"] = os.pathsep.join(str(p) for p in protected)

    prompt = INTERRUPTED_PROMPT.format(prompt=task.prompt) if task.attempts > 1 else task.prompt
    resume = session_exists(task.session_id)
    cmd = build_command(cfg, task.session_id, prompt, resume, model, effort)
    log.info("Görev %s başlıyor (resume=%s) %s", task.id, resume, task.project_dir)
    deadline = time.monotonic() + cfg.task_timeout_minutes * 60
    offset = log_file.stat().st_size if log_file.exists() else 0
    stats = LogStats()

    def scan() -> None:
        nonlocal offset
        offset, events = read_events(log_file, offset)
        before = stats.last_limit
        collect_stats(stats, events)
        if stats.last_limit is not before:
            on_limits(stats.last_limit)
    with log_file.open("ab") as out:
        proc = subprocess.Popen(
            cmd,
            cwd=task.project_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
        )
        while proc.poll() is None:
            _flush_outbox(agent_dir, on_outbox)
            scan()
            if stop_file.exists():
                _kill_tree(proc)
                stop_file.unlink(missing_ok=True)
                scan()
                return RunResult("stopped", "Görev elle durduruldu.", context_tokens=stats.context,
                                 limit_used=stats.limit_used, model=stats.model)
            if time.monotonic() > deadline:
                _kill_tree(proc)
                scan()
                return RunResult("timeout", f"{cfg.task_timeout_minutes} dakikalık süre aşıldı.",
                                 context_tokens=stats.context, limit_used=stats.limit_used, model=stats.model)
            time.sleep(2)
    _flush_outbox(agent_dir, on_outbox)
    scan()

    result = stats.result
    usage = {"cost_usd": result.get("total_cost_usd") if result else None, "context_tokens": stats.context,
             "tokens_in": stats.tokens_in, "tokens_out": stats.tokens_out, "limit_used": stats.limit_used,
             "model": stats.model, "model_usage": stats.model_usage}
    if result is None:
        return RunResult("failed", f"claude çıktı vermeden kapandı (kod {proc.returncode}). Log: {log_file}")
    final_text = str(result.get("result") or "")
    if result.get("is_error") or result.get("subtype") != "success":
        status = "rate_limited" if RATE_LIMIT_PATTERN.search(final_text) else "failed"
        return RunResult(status, final_text or str(result.get("subtype")), **usage)
    summary = _take(agent_dir / "summary.md") or final_text
    question = _take(agent_dir / "question.md")
    if question:
        return RunResult("waiting", summary, question=question, **usage)
    return RunResult("done", summary, **usage)


def zip_project(project_dir: Path, max_mb: int) -> Path | None:
    """Zips the project without heavy folders; None if it is too big to mail."""
    archive = project_dir.parent / f"{project_dir.name}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in project_dir.rglob("*"):
            rel = path.relative_to(project_dir)
            if path.is_file() and not ZIP_SKIP_DIRS.intersection(rel.parts):
                zf.write(path, rel)
    if archive.stat().st_size > max_mb * 1024 * 1024:
        archive.unlink()
        return None
    return archive
