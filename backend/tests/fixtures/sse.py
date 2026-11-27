import json
import re


def parse_sse(body: str) -> list[tuple[str, dict[str, object]]]:
    out = []
    for block in body.strip().split("\n\n"):
        m = re.fullmatch(r"event: (\S+)\ndata: (.*)", block, re.S)
        assert m, block
        out.append((m.group(1), json.loads(m.group(2))))
    return out
