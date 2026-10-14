"""Decides whether an incoming mail really comes from an allowed sender.

The From header is trivial to forge, so we also require that Gmail's own
receiving server (mx.google.com) verified the sender via DMARC or aligned DKIM.
Only the Authentication-Results header stamped by mx.google.com is trusted;
a sender can add fake ones further down the message.
"""

import re
from dataclasses import dataclass
from email.message import Message
from email.utils import parseaddr

TRUSTED_AUTHSERV_ID = "mx.google.com"


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    sender: str
    reason: str


def sender_address(msg: Message) -> str:
    return parseaddr(msg.get("From", ""))[1].strip().lower()


def _trusted_auth_results(msg: Message) -> str | None:
    for header in msg.get_all("Authentication-Results", []):
        value = " ".join(str(header).split())
        if value.lower().startswith(TRUSTED_AUTHSERV_ID):
            return value.lower()
    return None


def _domain_aligned(found: str, sender_domain: str) -> bool:
    return found == sender_domain or found.endswith("." + sender_domain)


def is_verified(auth_results: str, sender_domain: str) -> bool:
    dmarc = re.search(r"\bdmarc=pass\b[^;]*?header\.from=([\w.-]+)", auth_results)
    if dmarc and _domain_aligned(dmarc.group(1), sender_domain):
        return True
    for dkim in re.finditer(r"\bdkim=pass\b([^;]*)", auth_results):
        signer = re.search(r"header\.(?:d=|i=[^@\s]*@)([\w.-]+)", dkim.group(1))
        if signer and _domain_aligned(signer.group(1), sender_domain):
            return True
    return False


def check_mail(msg: Message, allowed: frozenset[str], secret_tag: str) -> AuthResult:
    sender = sender_address(msg)
    if sender not in allowed:
        return AuthResult(False, sender, "gönderen beyaz listede değil")
    auth_results = _trusted_auth_results(msg)
    if auth_results is None:
        return AuthResult(False, sender, "mx.google.com doğrulama başlığı yok")
    if not is_verified(auth_results, sender.rsplit("@", 1)[-1]):
        return AuthResult(False, sender, "DMARC/DKIM doğrulaması geçmedi (sahte olabilir)")
    if secret_tag and secret_tag not in msg.get("Subject", ""):
        return AuthResult(False, sender, "gizli etiket konuda yok")
    return AuthResult(True, sender, "ok")
