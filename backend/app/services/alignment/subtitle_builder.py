from typing import Any, Dict, List


def build_srt(segments: List[Dict[str, Any]]) -> str:
    def _fmt(t: float) -> str:
        t = max(0.0, float(t))
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int(round((t - int(t)) * 1000))
        if ms >= 1000:
            ms = 999
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    out = []
    for i, seg in enumerate(segments, start=1):
        out.append(str(i))
        out.append(f"{_fmt(seg['start'])} --> {_fmt(seg['end'])}")
        out.append(seg["text"])
        out.append("")
    return "\n".join(out)

