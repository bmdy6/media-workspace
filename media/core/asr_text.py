"""Remove ASR placeholders without inventing missing speech (stdlib only)."""
import re


class ASRTextCleaner:
    """Incremental filter; withhold marker prefixes and whitespace until decided.

    Normal text still streams immediately. Only a possible five-character marker
    and pending whitespace are delayed; no raw marker can escape in SSE deltas.
    """

    def __init__(self):
        self.prefix = ""
        self.spaces = ""
        self.removed = False
        self.started = False

    def _write(self, char: str) -> str:
        if char.isspace():
            self.spaces += char
            return ""
        spaces = re.sub(r"[ \t]+", " ", self.spaces) if self.removed else self.spaces
        result = (spaces if self.started else "") + char
        self.spaces = ""
        self.removed = False
        self.started = True
        return result

    def push(self, text: str) -> str:
        output = []
        for char in text:
            candidate = self.prefix + char
            if "<unk>".startswith(candidate.lower()):
                self.prefix = candidate
                if candidate.lower() == "<unk>":
                    self.prefix = ""
                    self.removed = True
                continue
            if self.prefix:
                for pending in self.prefix:
                    output.append(self._write(pending))
                self.prefix = ""
            if char == "<":
                self.prefix = char
            else:
                output.append(self._write(char))
        return "".join(output)

    def finish(self) -> str:
        result = "".join(self._write(char) for char in self.prefix)
        self.prefix = ""
        self.spaces = ""  # trailing whitespace, including an empty marker-only result
        return result


def clean_asr_text(text: str) -> str:
    cleaner = ASRTextCleaner()
    return cleaner.push(text) + cleaner.finish()
