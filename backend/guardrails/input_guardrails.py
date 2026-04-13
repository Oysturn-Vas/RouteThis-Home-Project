import re
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("routemaster-input-guardrails")


@dataclass
class InputGuardrailResult:
    is_safe: bool
    sanitized_text: str
    threat_type: Optional[str] = None
    details: Optional[str] = None


class InputGuardrails:
    INJECTION_PATTERNS = [
        r"<system-reminder>.*?</system-reminder>",
        r"</?system>",
        r"ignore\s+(all\s+)?previous\s+(instructions?|prompts?|commands?)",
        r"ignore\s+(all\s+)?instructions",
        r"(system|developer|assistant)\s*:\s*",
        r"{{.*?}}",
        r"\[INST\].*?\[/INST\]",
        r"<\|.*?\|>",
        r"(you\s+are\s+a|act\s+as\s+a|pretend\s+you\s+are|you\s+must\s+be\s+a)",
        r"(disregard|forget)\s+(everything|all|your)",
        r"forget\s+your\s+(system\s+)?(prompt|instructions|guidelines)",
        r"new\s+(system\s+)?instructions?:",
        r"override\s+(your\s+)?(safety|content\s+)?(policy|guidelines|instructions)",
        r"you\s+have\s+no\s+(safety|content\s+)?restrictions?",
    ]

    SENSITIVE_PATTERNS = [
        (r"\b\d{3}-\d{2}-\d{4}\b", "XXX-XX-XXXX"),
        (r"\b\d{16}\b", "****-****-****-****"),
        (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "***@***.***"),
    ]

    MAX_TEXT_LENGTH = 2000

    def __init__(self):
        self._injection_regex = re.compile(
            "|".join(self.INJECTION_PATTERNS),
            re.IGNORECASE | re.DOTALL
        )
        self._sensitive_regex = re.compile(
            "|".join(p[0] for p in self.SENSITIVE_PATTERNS),
            re.IGNORECASE
        )

    def check(self, text: str) -> InputGuardrailResult:
        injection_match = self._injection_regex.search(text)
        if injection_match:
            threat = injection_match.group()
            sanitized = self._injection_regex.sub("[REDACTED]", text)
            logger.warning(f"Prompt injection detected: {threat[:50]}...")
            return InputGuardrailResult(
                is_safe=True,
                sanitized_text=sanitized,
                threat_type="prompt_injection",
                details=f"Potential instruction override detected and sanitized"
            )
        return InputGuardrailResult(is_safe=True, sanitized_text=text)

    def mask_sensitive(self, text: str) -> tuple[str, bool]:
        masked = text
        had_sensitive = False
        for pattern, replacement in self.SENSITIVE_PATTERNS:
            if re.search(pattern, masked, re.IGNORECASE):
                masked = re.sub(pattern, replacement, masked, flags=re.IGNORECASE)
                had_sensitive = True
        if had_sensitive:
            logger.info("Sensitive content masked in user input")
        return masked, had_sensitive

    def check_length(self, text: str) -> InputGuardrailResult:
        if len(text) > self.MAX_TEXT_LENGTH:
            logger.warning(f"Input text exceeds {self.MAX_TEXT_LENGTH} chars, truncating")
            return InputGuardrailResult(
                is_safe=True,
                sanitized_text=text[:self.MAX_TEXT_LENGTH],
                threat_type="length_exceeded",
                details=f"Text truncated to {self.MAX_TEXT_LENGTH} characters"
            )
        return InputGuardrailResult(is_safe=True, sanitized_text=text)

    def process(self, text: str) -> InputGuardrailResult:
        result = self.check(text)
        result = InputGuardrailResult(
            is_safe=result.is_safe,
            sanitized_text=result.sanitized_text,
            threat_type=result.threat_type,
            details=result.details
        )
        
        masked_text, had_sensitive = self.mask_sensitive(result.sanitized_text)
        result = InputGuardrailResult(
            is_safe=result.is_safe,
            sanitized_text=masked_text,
            threat_type=result.threat_type,
            details=result.details
        )
        
        length_result = self.check_length(result.sanitized_text)
        result = InputGuardrailResult(
            is_safe=result.is_safe,
            sanitized_text=length_result.sanitized_text,
            threat_type=length_result.threat_type or result.threat_type,
            details=length_result.details or result.details
        )
        
        return result
