import re
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("routemaster-output-guardrails")


@dataclass
class OutputGuardrailResult:
    is_safe: bool
    sanitized_text: str
    was_modified: bool = False
    threat_type: Optional[str] = None
    details: Optional[str] = None


class OutputGuardrails:
    TOXIC_PATTERNS = [
        r"(unplug|disconnect)\s+(the\s+)?router\s+(and|to)\s+(smoke|fire|burn)",
        r"(smoke|fire|burning|sparks?)\s+(coming\s+)?from\s+(the\s+)?router",
        r"router\s+(is\s+)?(on\s+)?fire",
        r"electrical\s+(shock|hazard|danger)",
        r"(do\s+not|don't)\s+(unplug|disconnect)\s+(the\s+)?router",
        r"pour\s+(water|liquid)\s+(on|into)\s+(the\s+)?router",
        r"submerge\s+(the\s+)?router\s+(in\s+)?water",
        r"hit\s+(the\s+)?router\s+(with\s+)?(a\s+)?hammer",
        r"open\s+(the\s+)?router\s+(and\s+)?(touch|modify|change)\s+(the\s+)?(internals?|circuit|board|components?)",
    ]

    WARNING_PREFIX = "[Warning: Content modified for safety] "

    def __init__(self):
        self._toxic_regex = re.compile(
            "|".join(self.TOXIC_PATTERNS),
            re.IGNORECASE | re.DOTALL
        )

    def detect_toxic(self, text: str) -> Optional[str]:
        match = self._toxic_regex.search(text)
        return match.group() if match else None

    def _sanitize_toxic(self, text: str) -> str:
        sanitized = self._toxic_regex.sub("[SAFETY INSTRUCTION REDACTED]", text)
        return sanitized

    def check(self, text: str) -> OutputGuardrailResult:
        toxic_match = self.detect_toxic(text)
        if toxic_match:
            logger.warning(f"Toxic content detected: {toxic_match[:50]}...")
            sanitized = self._sanitize_toxic(text)
            return OutputGuardrailResult(
                is_safe=True,
                sanitized_text=sanitized,
                was_modified=True,
                threat_type="toxic_content",
                details="Potentially harmful instruction detected and sanitized"
            )
        return OutputGuardrailResult(is_safe=True, sanitized_text=text)

    def strip_markdown(self, text: str) -> str:
        text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"\*(.+?)\*", r"\1", text)
        text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^[\-\*]\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        return text.strip()

    def check_and_sanitize(self, text: str) -> OutputGuardrailResult:
        base_check = self.check(text)
        
        sanitized = self.strip_markdown(base_check.sanitized_text)
        
        was_modified = base_check.was_modified or (sanitized != base_check.sanitized_text)
        
        if base_check.was_modified:
            sanitized = self.WARNING_PREFIX + sanitized
        
        return OutputGuardrailResult(
            is_safe=base_check.is_safe,
            sanitized_text=sanitized.strip(),
            was_modified=was_modified,
            threat_type=base_check.threat_type,
            details=base_check.details
        )
